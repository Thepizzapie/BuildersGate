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
from pathlib import Path
from typing import Optional

from . import db, feedback
from .util import rows, slugify

# Live recordings, keyed by session id. Deliberately in-memory: a Recording owns
# an ffmpeg process and an audio stream, neither of which survives a restart.
# If the server dies mid-session, the session is marked failed, not resumed.
_LIVE: dict[int, object] = {}

SESSIONS_DIRNAME = "playtests"


def _session_dir(root, session_id: int, slug: str) -> Path:
    return Path(root) / db.DB_DIRNAME / SESSIONS_DIRNAME / f"{session_id:04d}-{slug}"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def preflight(mic_device: Optional[int] = None, window_title: Optional[str] = None) -> dict:
    """Check everything a session needs BEFORE committing to a playthrough."""
    from bgate_adapters import recorder, transcribe

    checks: dict = {}
    try:
        checks["ffmpeg"] = {"ok": True, "path": recorder.find_ffmpeg()}
    except Exception as exc:
        checks["ffmpeg"] = {"ok": False, "reason": str(exc)}

    checks["mic"] = recorder.probe_mic(mic_device)
    checks["transcriber"] = transcribe.available()

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
          build_ref: str = "", fps: int = 30) -> dict:
    """Begin recording. Raises if preflight fails — never records a doomed session."""
    from bgate_adapters import recorder

    slug = slugify(name)
    with db.tx(root) as conn:
        live = conn.execute(
            "SELECT id, name FROM playtest_session WHERE status = 'recording'").fetchone()
        if live:
            raise RuntimeError(
                f"session {live['id']} ({live['name']!r}) is already recording — "
                "stop it first; two ffmpeg captures fight over the same window"
            )
        cur = conn.execute(
            "INSERT INTO playtest_session (name, slug, status, game_cmd, build_ref) "
            "VALUES (?, ?, 'recording', ?, ?)",
            (name, slug, game_cmd, build_ref),
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
            "telemetry_path = ?, frames_dir = ? WHERE id = ?",
            (str(rec.video_path), str(rec.audio_path), str(telemetry),
             str(out_dir / "frames"), session_id),
        )
    return {
        "session_id": session_id,
        "name": name,
        "recording": True,
        "dir": str(out_dir),
        "telemetry_path": str(telemetry),
        "hint": "Play and talk. The game should append JSONL events to "
                "telemetry_path — see telemetry_contract().",
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
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE playtest_session SET status = 'processing', ended_at = datetime('now'), "
            "duration_s = ?, video_path = ?, audio_path = ? WHERE id = ?",
            (result["duration_s"], result["video_path"], result["audio_path"], session_id),
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


def transcribe_session(root: str | os.PathLike[str], session_id: int, *,
                       model: str = "base", audio_offset_s: float = 0.0) -> dict:
    """Transcribe, shift onto the session clock, extract items + frames."""
    from bgate_adapters import recorder, transcribe

    session = get(root, session_id)
    if not session["audio_path"]:
        return {"ok": False, "error": "session has no audio"}

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
            got = recorder.extract_frame(session["video_path"], item["t"], str(path))
            if got["ok"]:
                item["frame_path"] = got["path"]

    with db.tx(root) as conn:
        for item in items:
            conn.execute(
                "INSERT INTO playtest_item (session_id, segment_id, t, kind, text, seat, "
                "frame_path, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'new')",
                (session_id, item.get("segment_id"), item["t"], item["kind"],
                 item["text"], item["seat"], item["frame_path"]),
            )

    _ready(root, session_id)
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
        "path": "the session's telemetry_path (given by playtest_start)",
        "format": "JSONL — one JSON object per line, appended live",
        "required": {
            "t": "float, seconds since the game started (NOT wall clock)",
            "kind": "short event name: 'jump', 'death', 'fps', 'level_load'",
        },
        "optional": {"data": "object — any payload, e.g. {'height': 2.4, 'air_time': 0.9}"},
        "example": '{"t": 12.5, "kind": "jump", "data": {"air_time": 0.92, "peak_h": 2.4}}',
        "why": ("Joined to the transcript on the session clock, this is what turns "
                "'the jump feels floaty' into 'air_time 0.92s at t=12.5' — a number "
                "an agent can act on instead of a vibe it has to guess at."),
    }


def ingest_telemetry(root: str | os.PathLike[str], session_id: int) -> dict:
    """Read the game's JSONL into the event table. Bad lines are skipped, counted."""
    session = get(root, session_id)
    path = session["telemetry_path"]
    if not path or not Path(path).exists():
        return {"ingested": 0, "skipped": 0,
                "note": "no telemetry file — the game emitted nothing"}

    good, bad = [], 0
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            good.append((session_id, float(event["t"]), str(event["kind"]),
                         json.dumps(event.get("data", {}))))
        except Exception:
            bad += 1

    with db.tx(root) as conn:
        conn.execute("DELETE FROM playtest_event WHERE session_id = ?", (session_id,))
        conn.executemany(
            "INSERT INTO playtest_event (session_id, t, kind, data) VALUES (?, ?, ?, ?)",
            good)
    return {"ingested": len(good), "skipped": bad}


# ---------------------------------------------------------------------------
# The agent-facing artifact
# ---------------------------------------------------------------------------
def brief(root: str | os.PathLike[str], session_id: int, *,
          window_s: float = 4.0, include_transcript: bool = False) -> dict:
    """The session as agents consume it: items + frames + nearby telemetry.

    window_s: how far around an item to pull events. 4s covers "I say it right
    after it happens" without dragging in the whole level.
    """
    session = get(root, session_id)
    conn = db.connect(root)

    items = rows(conn.execute(
        "SELECT * FROM playtest_item WHERE session_id = ? ORDER BY t", (session_id,)))
    for item in items:
        item["events"] = rows(conn.execute(
            "SELECT t, kind, data FROM playtest_event WHERE session_id = ? "
            "AND t BETWEEN ? AND ? ORDER BY t",
            (session_id, item["t"] - window_s, item["t"] + window_s)))
        for event in item["events"]:
            try:
                event["data"] = json.loads(event["data"])
            except Exception:
                pass

    out = {
        "session": {k: session[k] for k in
                    ("id", "name", "status", "started_at", "duration_s",
                     "video_path", "build_ref")},
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
        "note": ("Frames are stills at each item's timestamp — agents cannot watch "
                 "the video; read frame_path. Items are 'new' until a human "
                 "promotes them; do not treat them as agreed work."),
    }
    if include_transcript:
        out["transcript"] = rows(conn.execute(
            "SELECT t_start, t_end, text FROM playtest_segment WHERE session_id = ? "
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
    return dict(row)


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
            "ended_at = COALESCE(ended_at, datetime('now')) WHERE id = ?",
            (error, session_id))


def _ready(root, session_id: int) -> None:
    with db.tx(root) as conn:
        conn.execute("UPDATE playtest_session SET status = 'ready' WHERE id = ?",
                     (session_id,))


def _tally(items: list[dict], field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        out[item[field]] = out.get(item[field], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
