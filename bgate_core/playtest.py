"""Playtest sessions — record, transcribe, align, brief.

The whole design turns on one fact: **agents cannot watch video**. The mp4 is for
the human. What the team consumes is the aligned artifact — transcript, frames
pulled at the moments you spoke, and game telemetry joined on the same clock.
That join is what makes "the jump feels floaty" actionable: it lands next to the
actual jump event at that timestamp.

One clock: every t_* is SECONDS FROM SESSION START. Whisper timestamps are
relative to the wav, so audio_offset_s is added on ingest, once, here.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import activity, db, feedback, iterations
from .util import rows, slugify

# Live recordings, keyed by session id. Deliberately in-memory: a Recording owns
# an ffmpeg process and an audio stream, neither of which survives a restart.
# If the server dies mid-session, the session is marked failed, not resumed.
_LIVE: dict[int, object] = {}
_GAMES: dict[int, subprocess.Popen] = {}
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

SESSIONS_DIRNAME = "playtests"


def _session_dir(root, session_id: int, slug: str) -> Path:
    return Path(root) / db.DB_DIRNAME / SESSIONS_DIRNAME / f"{session_id:04d}-{slug}"


def _build_identity(root: str | os.PathLike[str]) -> str:
    """Best-effort immutable identity for comparing playtest iterations."""
    project_root = Path(root)
    commit = "unversioned"
    dirty = False
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"], cwd=project_root,
            capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL)
        if proc.returncode == 0 and proc.stdout.strip():
            commit = proc.stdout.strip()
        dirty_proc = subprocess.run(
            ["git", "status", "--porcelain"], cwd=project_root,
            capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL)
        dirty = bool(dirty_proc.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    build = project_root / "export" / "web" / "index.pck"
    build_stamp = ""
    if build.is_file():
        from .assets import file_hash
        build_stamp = file_hash(build)[:12]
    return f"{commit}{'+dirty' if dirty else ''}{'@' + build_stamp if build_stamp else ''}"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def preflight(mic_device: Optional[int] = None, window_title: Optional[str] = None,
              *, root: Optional[str | os.PathLike[str]] = None,
              native: bool = False) -> dict:
    """Check everything a session needs BEFORE committing to a playthrough."""
    from bgate_adapters import recorder, transcribe

    checks: dict = {}
    try:
        checks["ffmpeg"] = {"ok": True, "path": recorder.find_ffmpeg()}
    except Exception as exc:
        checks["ffmpeg"] = {"ok": False, "reason": str(exc)}

    checks["mic"] = recorder.probe_mic(mic_device)
    checks["transcriber"] = transcribe.available()

    if native:
        from bgate_adapters import godot
        try:
            executable = godot.find_godot()
            project = Path(root or ".") / "game" / "project.godot"
            checks["native_game"] = {
                "ok": project.is_file(),
                "godot": executable,
                "project": str(project),
                "reason": "" if project.is_file() else "no game/project.godot",
            }
        except Exception as exc:
            checks["native_game"] = {"ok": False, "reason": str(exc)}

    if window_title:
        matches = recorder.list_windows(window_title)
        checks["window"] = {
            "ok": bool(matches),
            "matches": matches,
            "reason": "" if matches else f"no visible window matching {window_title!r} "
                                         "— start the game first",
        }

    ready = all(c.get("ok", c.get("available", False)) for c in checks.values())
    return {"ready": ready, "checks": checks}


def start(root: str | os.PathLike[str], name: str, *, window_title: Optional[str] = None,
          mic_device: Optional[int] = None, game_cmd: str = "",
          build_ref: str = "", fps: int = 30,
          launch_native: bool = False) -> dict:
    """Begin recording. Raises if preflight fails — never records a doomed session."""
    from bgate_adapters import recorder

    slug = slugify(name)
    build_ref = build_ref or _build_identity(root)
    live = db.connect(root).execute(
        "SELECT id, name FROM playtest_session WHERE status = 'recording'").fetchone()
    if live:
        raise RuntimeError(
            f"session {live['id']} ({live['name']!r}) is already recording — "
            "stop it first; two ffmpeg captures fight over the same window"
        )
    iteration = iterations.create(root, name)
    iteration_id = int(iteration["id"])
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO playtest_session "
            "(name, slug, status, game_cmd, build_ref, iteration_id) "
            "VALUES (?, ?, 'recording', ?, ?, ?)",
            (name, slug, game_cmd, build_ref, iteration_id),
        )
        session_id = int(cur.lastrowid)

    out_dir = _session_dir(root, session_id, slug)
    try:
        rec = recorder.start(out_dir, window_title=window_title,
                             mic_device=mic_device, fps=fps)
    except Exception as exc:
        with db.tx(root) as conn:
            conn.execute(
                "UPDATE playtest_session SET status = 'failed', error = ?, "
                "ended_at = datetime('now') WHERE id = ?",
                (str(exc), session_id),
            )
        raise

    _LIVE[session_id] = rec
    telemetry = out_dir / "telemetry.jsonl"
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE playtest_session SET video_path = ?, audio_path = ?, "
            "telemetry_path = ?, frames_dir = ?, started_epoch = ? WHERE id = ?",
            (str(rec.video_path), str(rec.audio_path), str(telemetry),
             str(out_dir / "frames"), rec.started_at, session_id),
        )
    native = None
    if launch_native:
        try:
            native = launch_native_game(
                root, session_id, str(telemetry), game_cmd=game_cmd)
        except Exception:
            _LIVE.pop(session_id, None)
            try:
                recorder.stop(rec)
            except Exception:
                pass
            with db.tx(root) as conn:
                conn.execute(
                    "UPDATE playtest_session SET status = 'failed', "
                    "error = 'native game launch failed', ended_at = datetime('now') "
                    "WHERE id = ?", (session_id,))
            raise
    activity.log(root, "playtest", f"recording session {name!r}",
                 ref=str(session_id))
    iterations.add_event(
        root, iteration_id, "playtest", "playtest", str(session_id),
        f"Started playtest {name}", {"build_ref": build_ref})
    return {
        "session_id": session_id,
        "name": name,
        "recording": True,
        "build_ref": build_ref,
        "iteration_id": iteration_id,
        "dir": str(out_dir),
        "telemetry_path": str(telemetry),
        "env": {"BGATE_TELEMETRY": str(telemetry)},
        "native_launch": native,
        "hint": "Launch the game with BGATE_TELEMETRY set to telemetry_path (the "
                "BGate autoload reads it). Then play and TALK — say what you like "
                "and what needs fixing, right when it happens.",
    }


def stop(root: str | os.PathLike[str], session_id: Optional[int] = None, *,
         model: str = "base", transcribe_now: bool = True) -> dict:
    """End recording, then transcribe + align + classify into a brief."""
    from bgate_adapters import recorder

    session = _active(root, session_id)
    session_id = session["id"]
    rec = _LIVE.pop(session_id, None)
    if rec is None:
        _fail(root, session_id, "no live recorder — server restarted mid-session?")
        raise RuntimeError(
            f"session {session_id} has no live recorder in this process "
            "(the server restarted). Marked failed; the partial files remain on disk."
        )

    result = recorder.stop(rec)
    game_proc = _GAMES.pop(session_id, None)
    if game_proc is not None and game_proc.poll() is None:
        game_proc.terminate()
        try:
            game_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            game_proc.kill()
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE playtest_session SET status = 'processing', ended_at = datetime('now'), "
            "processing_stage = 'transcribing', processing_error = '', "
            "duration_s = ?, video_path = ?, audio_path = ?, "
            "audio_offset_s = ?, video_offset_s = ? WHERE id = ?",
            (result["duration_s"], result["video_path"], result["audio_path"],
             result["audio_offset_s"], result["video_offset_s"], session_id),
        )

    summary = {
        "session_id": session_id,
        "duration_s": result["duration_s"],
        "video": result["video_path"],
        "video_ok": result["video_ok"],
        "audio": result["audio_path"],
        "warnings": result["warnings"],
    }
    if result["video_error"]:
        summary["video_error"] = result["video_error"]

    events = ingest_telemetry(root, session_id)
    summary["telemetry_events"] = events["ingested"]

    if not transcribe_now:
        _ready(root, session_id)
        return summary

    if not result["audio_path"]:
        _fail(root, session_id, "no audio captured")
        summary["transcript"] = {"ok": False, "error": "no audio captured"}
        return summary

    summary["transcript"] = transcribe_session(
        root, session_id, model=model, audio_offset_s=result["audio_offset_s"])
    return summary


def launch_native_game(root: str | os.PathLike[str], session_id: int,
                       telemetry_path: str, *, game_cmd: str = "") -> dict:
    """Launch the native Godot project with telemetry owned by this session."""
    game_dir = Path(root) / "game"
    if game_cmd:
        args = shlex.split(game_cmd, posix=os.name != "nt")
    else:
        if not (game_dir / "project.godot").is_file():
            raise RuntimeError("no native Godot project at <root>/game")
        from bgate_adapters import godot
        executable = godot.find_godot()
        args = [executable, "--path", str(game_dir)]
    if not args:
        raise ValueError("native game command is empty")

    env = os.environ.copy()
    env["BGATE_TELEMETRY"] = telemetry_path
    proc = subprocess.Popen(
        args, cwd=str(game_dir if game_dir.is_dir() else Path(root)),
        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    _GAMES[session_id] = proc
    rendered = subprocess.list2cmdline(args)
    with db.tx(root) as conn:
        conn.execute("UPDATE playtest_session SET game_cmd = ? WHERE id = ?",
                     (rendered, session_id))
    activity.log(root, "playtest", f"launched native game for session {session_id}",
                 ref=str(session_id))
    return {"pid": proc.pid, "command": rendered,
            "telemetry_path": telemetry_path}


def transcribe_session(root: str | os.PathLike[str], session_id: int, *,
                       model: str = "base", audio_offset_s: float = 0.0) -> dict:
    """Transcribe, shift onto the session clock, extract items + frames."""
    from bgate_adapters import recorder, transcribe

    session = get(root, session_id)
    if not session["audio_path"]:
        return {"ok": False, "error": "session has no audio"}

    with db.tx(root) as conn:
        conn.execute(
            "UPDATE playtest_session SET status = 'processing', "
            "processing_stage = 'transcribing', processing_error = '' WHERE id = ?",
            (session_id,))

    result = transcribe.transcribe(session["audio_path"], model=model)
    if not result.get("ok"):
        _fail(root, session_id, result.get("error", "transcription failed"))
        return result

    # Whisper timestamps are relative to the WAV. The mic stream started a beat
    # after the session did; correct once, here, so nothing downstream has to.
    segments = []
    for seg in result["segments"]:
        segments.append({**seg,
                         "t_start": round(seg["t_start"] + audio_offset_s, 3),
                         "t_end": round(seg["t_end"] + audio_offset_s, 3)})

    with db.tx(root) as conn:
        conn.execute("DELETE FROM playtest_segment WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM playtest_item WHERE session_id = ? AND status = 'new'",
                     (session_id,))
        for seg in segments:
            cur = conn.execute(
                "INSERT INTO playtest_segment (session_id, t_start, t_end, text, confidence) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, seg["t_start"], seg["t_end"], seg["text"], seg.get("confidence")),
            )
            seg["id"] = int(cur.lastrowid)

    items = feedback.extract(segments)

    # A frame per item is what an agent actually "sees". Only for real items —
    # extracting one per segment would burn minutes on filler.
    frames_dir = Path(session["frames_dir"] or (Path(session["video_path"]).parent / "frames"))
    for item in items:
        item["frame_path"] = None
        if session["video_path"] and Path(session["video_path"]).exists():
            path = frames_dir / f"t{item['t']:07.2f}.jpg".replace(" ", "0")
            video_t = max(0.0, item["t"] - float(session["video_offset_s"] or 0))
            got = recorder.extract_frame(session["video_path"], video_t, str(path))
            if got["ok"]:
                item["frame_path"] = got["path"]

    with db.tx(root) as conn:
        logical_assets = [
            row[0] for row in conn.execute(
                "SELECT DISTINCT logical_name FROM artifact_revision")
        ]
        for item in items:
            recommendation = (
                "promote" if item["kind"] in ("fix", "add", "change")
                and item["seat"] != "unassigned" else
                "keep" if item["kind"] == "like" else "review"
            )
            cur = conn.execute(
                "INSERT INTO playtest_item (session_id, segment_id, t, kind, text, seat, "
                "frame_path, status, director_recommendation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?)",
                (session_id, item.get("segment_id"), item["t"], item["kind"],
                 item["text"], item["seat"], item["frame_path"], recommendation),
            )
            item_id = int(cur.lastrowid)
            normalized = item["text"].lower().replace("_", " ").replace("-", " ")
            for logical_name in logical_assets:
                needle = logical_name.lower().replace("_", " ").replace("-", " ")
                if needle and needle in normalized:
                    conn.execute(
                        "INSERT OR IGNORE INTO playtest_item_asset "
                        "(item_id, logical_name, confidence) VALUES (?, ?, .65)",
                        (item_id, logical_name))

    _ready(root, session_id)
    activity.log(root, "playtest",
                 f"session {session_id} transcribed: {len(items)} feedback items",
                 ref=str(session_id))
    if session.get("iteration_id"):
        iterations.add_event(
            root, int(session["iteration_id"]), "review", "playtest",
            str(session_id), f"Extracted {len(items)} feedback items",
            {"items": len(items), "by_kind": _tally(items, "kind"),
             "by_seat": _tally(items, "seat")})
    return {
        "ok": True,
        "segments": len(segments),
        "items": len(items),
        "language": result.get("language"),
        "by_kind": _tally(items, "kind"),
        "by_seat": _tally(items, "seat"),
    }


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------
def telemetry_contract() -> dict:
    """What the game must emit for feedback to become actionable."""
    return {
        "easiest": ("scaffold a project with godot_scaffold — the BGate telemetry "
                    "autoload already does all of this. Then just call "
                    "BGateTelemetry.emit_event(kind, data) from your game code."),
        "path": "env var BGATE_TELEMETRY (given by playtest_start)",
        "format": "JSONL — one JSON object per line, appended and flushed live",
        "required": {
            "ts": "float, UNIX WALL-CLOCK seconds (Time.get_unix_time_from_system()). "
                  "NOT seconds-since-game-start: the game's clock and the recorder's "
                  "are unrelated, and wall clock is the only shared axis.",
            "kind": "short event name: 'jump', 'death', 'fps', 'level_load'",
        },
        "optional": {
            "schema": "integer telemetry schema version; current version is 1",
            "data": "object — any payload, e.g. {'air_time': 0.92, 'peak_h': 2.4}",
            "t": "float, seconds since game start — for humans reading the file",
        },
        "example": '{"schema": 1, "ts": 1752694812.44, "t": 12.5, "kind": "jump", '
                   '"data": {"air_time": 0.92, "peak_h": 2.4}}',
        "why": ("Joined to the transcript on the session clock, this is what turns "
                "'the jump feels floaty' into 'air_time 0.92s' — a number an agent "
                "can act on instead of a vibe it has to guess at."),
        "flush": ("flush on a timer. Godot buffers, and a crash would lose exactly "
                  "the events that explain the crash."),
    }


def ingest_telemetry(root: str | os.PathLike[str], session_id: int) -> dict:
    """Read the game's JSONL into the event table, ON THE SESSION CLOCK.

    Events carry `ts` (unix wall clock) because the game's own clock is unrelated
    to the recorder's — the game may have been running for an hour before you hit
    record. `ts - started_epoch` is the only correct conversion.

    Falls back to a raw `t` when `ts` is absent, which ASSUMES the game and the
    session started together. That's usually wrong, so it's reported, not hidden.
    """
    session = get(root, session_id)
    path = session["telemetry_path"]
    if not path or not Path(path).exists():
        return {"ingested": 0, "skipped": 0,
                "note": "no telemetry file — the game emitted nothing"}

    anchor = session["started_epoch"]
    good, bad, assumed = [], 0, 0
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            if "ts" in event and anchor:
                t = float(event["ts"]) - float(anchor)
            elif "t" in event:
                t = float(event["t"])
                assumed += 1
            else:
                bad += 1
                continue
            good.append((session_id, t, str(event["kind"]),
                         json.dumps(event.get("data", {}))))
        except Exception:
            bad += 1

    with db.tx(root) as conn:
        conn.execute("DELETE FROM playtest_event WHERE session_id = ?", (session_id,))
        conn.executemany(
            "INSERT INTO playtest_event (session_id, t, kind, data) VALUES (?, ?, ?, ?)",
            good)

    out = {"ingested": len(good), "skipped": bad}
    if assumed:
        out["warning"] = (
            f"{assumed} event(s) had no 'ts' — their timestamps assume the game "
            "started exactly when recording did. Use the BGate telemetry autoload, "
            "which emits wall-clock ts."
        )
    if good and anchor is None:
        out["warning"] = ("session has no started_epoch anchor (recorded before "
                          "this was tracked) — telemetry alignment is unreliable")
    return out


def ingest_web_event(root: str | os.PathLike[str], session_id: int,
                     event: dict) -> dict:
    """Persist one event posted by an in-browser Godot build."""
    session = get(root, session_id)
    if session["status"] not in ("recording", "processing"):
        raise RuntimeError(
            f"session {session_id} is {session['status']}; telemetry is closed")
    if "kind" not in event or not str(event["kind"]).strip():
        raise ValueError("telemetry event needs a kind")
    anchor = session["started_epoch"]
    if "ts" in event and anchor:
        t = float(event["ts"]) - float(anchor)
    elif "t" in event:
        t = float(event["t"])
    else:
        raise ValueError("telemetry event needs ts or t")
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO playtest_event (session_id, t, kind, data) "
            "VALUES (?, ?, ?, ?)",
            (session_id, t, str(event["kind"])[:80],
             json.dumps(event.get("data", {}))),
        )
        event_id = int(cur.lastrowid)
    return {"ok": True, "id": event_id, "session_id": session_id, "t": t}


# ---------------------------------------------------------------------------
# The agent-facing artifact
# ---------------------------------------------------------------------------
def telemetry_summary(root: str | os.PathLike[str], session_id: int) -> dict:
    """The whole recorded event stream, distilled for review.

    A raw dump is 100+ fps ticks and 180 mid-drag slider values — noise. This
    collapses it into what a reviewer (or the director) actually needs:

      * settings — the NET tuning change per property: the value you landed on,
        where you started, how many nudges it took, and when you last touched
        it. This is the point of tuning live during a playtest; it turns "I
        changed the CPU damage" into "damage_scale 1.0 -> 0.75 @ 3:41".
      * moments — the discrete gameplay beats (jumps, hits, KOs, round starts)
        placed on the timeline, minus the fps/heartbeat spam.
      * fps — min/avg so "it tanked" has a number.
      * by_kind — the full tally, nothing hidden.
    """
    conn = db.connect(root)
    events = rows(conn.execute(
        "SELECT t, kind, data FROM playtest_event WHERE session_id = ? ORDER BY t",
        (session_id,)))
    for event in events:
        try:
            event["data"] = json.loads(event["data"])
        except Exception:
            event["data"] = {}

    settings: dict[str, dict] = {}
    moments: list[dict] = []
    fps_vals: list[float] = []
    by_kind: dict[str, int] = {}
    NOISE = {"fps", "session_open", "session_close", "autoquit"}
    for event in events:
        kind = event["kind"]
        by_kind[kind] = by_kind.get(kind, 0) + 1
        data = event["data"] if isinstance(event["data"], dict) else {}
        if kind == "fps":
            try:
                fps_vals.append(float(data.get("fps")))
            except (TypeError, ValueError):
                pass
        elif kind == "setting_changed":
            key = str(data.get("key") or data.get("prop") or "?")
            entry = settings.get(key)
            if entry is None:
                settings[key] = {
                    "key": key, "prop": data.get("prop"),
                    "group": data.get("group"),
                    "from": data.get("value"), "to": data.get("value"),
                    "count": 1, "t_first": event["t"], "t": event["t"]}
            else:
                entry["to"] = data.get("value")
                entry["count"] += 1
                entry["t"] = event["t"]
        elif kind not in NOISE:
            moments.append({"t": event["t"], "kind": kind, "data": data})

    # A property nudged back to where it started is not a change worth showing.
    changed = [s for s in settings.values()
               if s["from"] != s["to"] or s["count"] > 1]
    changed.sort(key=lambda s: s["t"])
    return {
        "by_kind": by_kind,
        "settings": changed,
        "moments": moments,
        "fps": ({"min": round(min(fps_vals), 1), "avg": round(sum(fps_vals) / len(fps_vals), 1),
                 "max": round(max(fps_vals), 1), "samples": len(fps_vals)}
                if fps_vals else None),
        "total": len(events),
    }


def _ensure_filmstrip(session: dict) -> list[dict]:
    """Frames spanning the whole video — the director's way of watching it.

    Idempotent: extract once into <video>/strip, reuse on later calls (the
    review endpoint is polled). Returns [] when there is no playable video.
    """
    vp = session.get("video_path")
    if not vp or not Path(vp).is_file():
        return []
    dur = max(float(session.get("duration_s") or 0.0), 4.0)
    step = max(4.0, dur / 90)  # must match recorder.extract_filmstrip's formula
    strip_dir = Path(vp).parent / "strip"
    existing = sorted(strip_dir.glob("strip_*.jpg"))
    if existing:
        return [{"i": i, "t": round(step * (i + 0.5), 2), "path": str(p)}
                for i, p in enumerate(existing)]
    try:
        return recorder.extract_filmstrip(vp, str(strip_dir), duration_s=dur)
    except Exception:
        return []


def brief(root: str | os.PathLike[str], session_id: int, *,
          window_s: float = 4.0, include_transcript: bool = False) -> dict:
    """The session as agents consume it: items + frames + nearby telemetry.

    window_s: how far around an item to pull events. 4s covers "I say it right
    after it happens" without dragging in the whole level.
    """
    session = get(root, session_id)
    conn = db.connect(root)

    items = rows(conn.execute(
        "SELECT i.*, s.confidence AS transcript_confidence "
        "FROM playtest_item i "
        "LEFT JOIN playtest_segment s ON s.id = i.segment_id "
        "WHERE i.session_id = ? ORDER BY i.t", (session_id,)))
    for item in items:
        classified_kind, scores = feedback.classify(item["text"])
        score_total = sum(scores.values())
        item["classification"] = {
            "kind": classified_kind,
            "confidence": (
                round(max(scores.values()) / score_total, 3)
                if score_total else 0.0),
            "scores": scores,
            "seat": feedback.route(item["text"]),
        }
        item["events"] = rows(conn.execute(
            "SELECT t, kind, data FROM playtest_event WHERE session_id = ? "
            "AND t BETWEEN ? AND ? ORDER BY t",
            (session_id, item["t"] - window_s, item["t"] + window_s)))
        for event in item["events"]:
            try:
                event["data"] = json.loads(event["data"])
            except Exception:
                pass
        work = conn.execute(
            "SELECT id, seat, title, status, result, updated_at "
            "FROM work_item WHERE source = 'playtest' AND source_ref = ? "
            "ORDER BY id DESC LIMIT 1", (str(item["id"]),)).fetchone()
        item["work"] = dict(work) if work else None
        item["assets"] = rows(conn.execute(
            "SELECT logical_name, confidence FROM playtest_item_asset "
            "WHERE item_id = ? ORDER BY logical_name", (item["id"],)))

    out = {
        "session": {k: session[k] for k in
                    ("id", "name", "status", "started_at", "duration_s",
                     "video_path", "audio_path", "build_ref", "iteration_id")},
        "counts": {
            "items": len(items),
            "events": conn.execute(
                "SELECT count(*) FROM playtest_event WHERE session_id = ?",
                (session_id,)).fetchone()[0],
            "segments": conn.execute(
                "SELECT count(*) FROM playtest_segment WHERE session_id = ?",
                (session_id,)).fetchone()[0],
        },
        "by_kind": _tally(items, "kind"),
        "by_seat": _tally(items, "seat"),
        "items": items,
        "timeline_markers": [
            {"item_id": item["id"], "t": item["t"], "kind": item["kind"],
             "status": item["status"], "text": item["text"]}
            for item in items
        ],
        "note": ("Frames are stills at each item's timestamp — agents cannot watch "
                 "the video; read frame_path. Items are 'new' until a human "
                 "promotes them; do not treat them as agreed work."),
    }
    out["telemetry_backed"] = out["counts"]["events"] > 0
    out["telemetry"] = telemetry_summary(root, session_id)
    out["video_frames"] = _ensure_filmstrip(session)
    iteration = None
    if session.get("iteration_id"):
        try:
            iteration = iterations.get(root, int(session["iteration_id"]))
        except LookupError:
            pass
    out["iteration"] = iteration
    warnings = []
    if not out["telemetry_backed"]:
        warnings.append({"kind": "no_telemetry", "message": "No game events arrived."})
    if not session.get("audio_path") or not Path(session["audio_path"]).is_file():
        warnings.append({"kind": "no_audio", "message": "No captured audio is available."})
    missing_frames = sum(1 for item in items
                         if not item.get("frame_path")
                         or not Path(item["frame_path"]).is_file())
    if missing_frames:
        warnings.append({
            "kind": "missing_frames",
            "message": f"{missing_frames} feedback items have no captured frame."})
    if iteration:
        current_source = iterations.snapshot(root)["source_fingerprint"]
        if current_source != iteration["source_fingerprint"]:
            warnings.append({
                "kind": "stale_build",
                "message": "Source changed after this playtest snapshot."})
        if iteration.get("tests", {}).get("status") not in ("passed", "pass", "ok"):
            warnings.append({
                "kind": "tests_not_captured",
                "message": "No passing automated-check snapshot is attached."})
    out["coverage_warnings"] = warnings
    if include_transcript:
        out["transcript"] = rows(conn.execute(
            "SELECT t_start, t_end, text, confidence "
            "FROM playtest_segment WHERE session_id = ? "
            "ORDER BY t_start", (session_id,)))
    return out


def promote(root: str | os.PathLike[str], item_id: int, *, seat: Optional[str] = None,
            kind: Optional[str] = None, ref: str = "") -> dict:
    """Accept a feedback item as real work. The human's call, never the model's."""
    conn = db.connect(root)
    row = conn.execute("SELECT * FROM playtest_item WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise LookupError(f"no playtest item {item_id}")
    if seat and seat not in feedback.SEATS:
        raise ValueError(f"seat must be one of {feedback.SEATS}, got {seat!r}")
    if kind and kind not in feedback.KINDS:
        raise ValueError(f"kind must be one of {feedback.KINDS}, got {kind!r}")

    with db.tx(root) as conn:
        conn.execute(
            "UPDATE playtest_item SET status = 'promoted', seat = ?, kind = ?, "
            "promoted_ref = ? WHERE id = ?",
            (seat or row["seat"], kind or row["kind"], ref, item_id),
        )
    activity.log(root, "promote",
                 f"promoted to {seat or row['seat']}: {row['text'][:80]}",
                 seat=seat or row["seat"], ref=str(item_id))
    iteration_id = _iteration_for_item(root, item_id)
    if iteration_id:
        iterations.add_event(
            root, iteration_id, "decision", "feedback", str(item_id),
            f"Promoted feedback to {seat or row['seat']}",
            {"disposition": "promoted", "kind": kind or row["kind"],
             "seat": seat or row["seat"]})
    return dict(db.connect(root).execute(
        "SELECT * FROM playtest_item WHERE id = ?", (item_id,)).fetchone())


def dismiss(root: str | os.PathLike[str], item_id: int) -> dict:
    with db.tx(root) as conn:
        conn.execute("UPDATE playtest_item SET status = 'dismissed' WHERE id = ?",
                     (item_id,))
    row = db.connect(root).execute(
        "SELECT * FROM playtest_item WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise LookupError(f"no playtest item {item_id}")
    iteration_id = _iteration_for_item(root, item_id)
    if iteration_id:
        iterations.add_event(
            root, iteration_id, "decision", "feedback", str(item_id),
            "Dismissed feedback", {"disposition": "dismissed"})
    return dict(row)


def merge(root: str | os.PathLike[str], item_id: int, target_id: int) -> dict:
    """Merge a duplicate into another item without erasing either record."""
    if item_id == target_id:
        raise ValueError("an item cannot merge into itself")
    conn = db.connect(root)
    source = conn.execute(
        "SELECT * FROM playtest_item WHERE id = ?", (item_id,)).fetchone()
    target = conn.execute(
        "SELECT * FROM playtest_item WHERE id = ?", (target_id,)).fetchone()
    if source is None or target is None:
        raise LookupError("source or target feedback item does not exist")
    if source["session_id"] != target["session_id"]:
        raise ValueError("feedback can only merge within one playtest session")
    with db.tx(root) as tx:
        tx.execute(
            "UPDATE playtest_item SET status = 'dismissed', merged_into_id = ? "
            "WHERE id = ?", (target_id, item_id))
    iteration_id = _iteration_for_item(root, item_id)
    if iteration_id:
        iterations.add_event(
            root, iteration_id, "decision", "feedback", str(item_id),
            f"Merged feedback into item {target_id}",
            {"disposition": "merged", "target_id": target_id})
    return dict(db.connect(root).execute(
        "SELECT * FROM playtest_item WHERE id = ?", (item_id,)).fetchone())


def _iteration_for_item(root: str | os.PathLike[str], item_id: int) -> Optional[int]:
    row = db.connect(root).execute(
        "SELECT s.iteration_id FROM playtest_item i "
        "JOIN playtest_session s ON s.id = i.session_id WHERE i.id = ?",
        (item_id,)).fetchone()
    return int(row["iteration_id"]) if row and row["iteration_id"] else None


# ---------------------------------------------------------------------------
# Queries + internals
# ---------------------------------------------------------------------------
def get(root: str | os.PathLike[str], session_id: int) -> dict:
    row = db.connect(root).execute(
        "SELECT * FROM playtest_session WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise LookupError(f"no playtest session {session_id}")
    return dict(row)


def list_sessions(root: str | os.PathLike[str], status: Optional[str] = None) -> list[dict]:
    conn = db.connect(root)
    if status:
        return rows(conn.execute(
            "SELECT * FROM playtest_session WHERE status = ? ORDER BY id DESC", (status,)))
    return rows(conn.execute("SELECT * FROM playtest_session ORDER BY id DESC"))


def _active(root, session_id: Optional[int]) -> dict:
    if session_id is not None:
        return get(root, session_id)
    row = db.connect(root).execute(
        "SELECT * FROM playtest_session WHERE status = 'recording' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        raise LookupError("no session is currently recording")
    return dict(row)


def _fail(root, session_id: int, error: str) -> None:
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE playtest_session SET status = 'failed', error = ?, "
            "processing_error = ?, processing_stage = 'failed', "
            "ended_at = COALESCE(ended_at, datetime('now')) WHERE id = ?",
            (error, error, session_id))
    session = get(root, session_id)
    if session.get("iteration_id"):
        iterations.add_event(
            root, int(session["iteration_id"]), "failure", "playtest",
            str(session_id), error, {"error": error})


def _ready(root, session_id: int) -> None:
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE playtest_session SET status = 'ready', "
            "processing_stage = 'ready', processing_error = '' WHERE id = ?",
                     (session_id,))


def _tally(items: list[dict], field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        out[item[field]] = out.get(item[field], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
