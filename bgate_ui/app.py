"""The dashboard backend — the local cockpit over the project's SQLite store.

One page, polled JSON, no build step, no node, no CDN. Mutations are deliberately
limited to user-facing orchestration and review: queue/dispatch, recording,
feedback disposition, and generated-artifact approval.

Run: bgate serve [--port 7788]   (from anywhere inside a project, or BGATE_ROOT)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from bgate_core import (
    activity, artifacts, assets, bible, db, lore, playtest,
    project, scaffold, seats,
)
from bgate_core import controls as _controls
from bgate_core import queue as _queue
from bgate_core.util import rows as _rows
from bgate_ui import api as _api
from bgate_ui import autodeploy as _autodeploy
from bgate_ui import redact as _redact
from bgate_ui import dispatch as _dispatch
from bgate_ui import followup as _followup
from bgate_ui import steerpump as _steerpump
from bgate_ui import routes as _routes

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Called directly rather than through a threadpool, which is exactly what
    # the `on_event("startup")` decorator this replaced did with a sync handler:
    # Starlette awaits async startup handlers and calls sync ones inline. Every
    # step below is already individually fail-safe, so an inline call cannot
    # wedge the boot.
    _start_reactors()
    yield


app = FastAPI(title="builders-gate-ui", docs_url=None, redoc_url=None,
              lifespan=_lifespan)

# One error envelope for every failure — see bgate_ui/api.py. Installed before
# anything else so even a startup-time raise comes back as parseable JSON.
_api.install_error_handlers(app)


def _start_reactors() -> None:
    # The follow-up router: ONE subscriber on the event bus that owns everything
    # that happens after an agent finishes — the auto-QA gate (which used to have
    # its own thread and its own "only since the server started" cutoff), the
    # chain handoff narration, the notice/webhook fan-out, the time-based stall
    # rules, and the opt-in director debrief. Fail-safe: a missing project just
    # means nothing is routed.
    try:
        _followup.start(str(_root()))
    except Exception:
        pass
    # Auto-deploy: the loop that dispatches queued work so a delegation's
    # children do not sit waiting for someone to press a button seven times.
    # The thread starts regardless — it reads the per-project switch every tick,
    # so turning it on in the browser must not need a restart.
    try:
        _autodeploy.start(str(_root()))
    except Exception:
        pass
    # The steer pump: only this process holds the agents' stdin, so it is the
    # only one that can deliver a message the director (or the MCP server, or a
    # second dashboard) left for a running agent.
    try:
        _steerpump.start(str(_root()))
    except Exception:
        pass
    # Sweep seat agents orphaned by a previous server run (their claude.exe
    # trees outlive the server that spawned them).
    try:
        swept = _dispatch.reap_orphans(str(_root()))
        if swept.get("killed"):
            activity.log(str(_root()), "dispatch",
                         f"reaped {len(swept['killed'])} orphaned agent(s)")
    except Exception:
        pass
    # Same problem one layer over: a recording ffmpeg outlives the server too.
    try:
        _repair_orphan_recordings(_root())
    except Exception:
        pass
    # Settle anything the last run left mid-flight. status() is a pure read now,
    # so nothing else will do this on a poll — an item stranded in 'dispatched'
    # would otherwise sit there until someone dispatched it again.
    try:
        _dispatch.sweep(str(_root()))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Orphaned recordings — a restart mid-session leaves ffmpeg running
# ---------------------------------------------------------------------------
# recorder.start() holds ffmpeg's stdin so stop() can send 'q' and let it write
# the moov atom. Kill the server instead and BOTH halves are lost: ffmpeg keeps
# capturing the desktop forever against a session nobody will ever stop, and the
# mp4 it is writing has no index, so the review overlay offers a video that no
# player can open. The session row, meanwhile, still says 'recording' — which
# blocks the next playtest.start() ("already recording") for good.
#
# So a restart reaps it: kill the capture, try to salvage the file, and tell the
# truth about the session instead of leaving all three states lying.

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_ORPHAN_REASON = ("the server restarted while this session was recording — "
                  "the capture was reaped and the audio was never written")


def _ffmpeg_pids_for(needle: str) -> list[int]:
    """PIDs of ffmpeg processes whose command line mentions ``needle``.

    tasklist cannot show a command line and the pid was never persisted, so the
    output path IS the identity — matching it is what keeps this from killing
    an unrelated ffmpeg the user is running. Best effort: no CIM, no kills.
    """
    import json as _json

    script = ("Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe'\" | "
              "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=20,
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW).stdout
        data = _json.loads(out.strip() or "[]")
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    want = needle.lower()
    return [int(row["ProcessId"]) for row in data
            if isinstance(row, dict) and row.get("ProcessId")
            and want in (row.get("CommandLine") or "").lower()]


def _kill_ffmpeg(pid: int) -> None:
    """Ask, then insist. /T because gdigrab capture is the whole tree."""
    for args in (["taskkill", "/PID", str(pid), "/T"],
                 ["taskkill", "/PID", str(pid), "/T", "/F"]):
        try:
            done = subprocess.run(args, capture_output=True, text=True,
                                  timeout=20, creationflags=_NO_WINDOW)
            if done.returncode == 0:
                return
        except Exception:
            return


def _finalise_orphan_video(path: Path) -> bool:
    """Remux an unfinalised mp4 in place. True when it is playable afterwards.

    ffmpeg can rebuild an index from the raw stream it already wrote, which is
    the difference between a recoverable session and a 300 MB file the browser
    reports as corrupt. If it cannot, we say so rather than serving the wreck.
    """
    try:
        from bgate_adapters.recorder import find_ffmpeg
        ffmpeg = find_ffmpeg()
    except Exception:
        return False
    if not path.is_file() or path.stat().st_size == 0:
        return False
    fixed = path.with_suffix(".repaired.mp4")
    try:
        done = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-err_detect", "ignore_err",
             "-i", str(path), "-c", "copy", "-movflags", "+faststart",
             str(fixed)],
            capture_output=True, text=True, timeout=300,
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except Exception:
        return False
    if done.returncode != 0 or not fixed.is_file() or fixed.stat().st_size == 0:
        fixed.unlink(missing_ok=True)
        return False
    try:
        fixed.replace(path)
    except OSError:
        return False
    return True


def _repair_orphan_recordings(root: Path) -> list[dict]:
    """Close out every session still marked 'recording' at startup.

    Nothing can legitimately be recording when the process that owned the
    capture has just started: playtest keeps its handles in memory.
    """
    try:
        stuck = _rows(db.connect(root).execute(
            "SELECT id, name, video_path FROM playtest_session "
            "WHERE status = 'recording'"))
    except Exception:
        return []
    repaired = []
    for session in stuck:
        video = (session["video_path"] or "").strip()
        killed = []
        if video:
            for pid in _ffmpeg_pids_for(video):
                _kill_ffmpeg(pid)
                killed.append(pid)
        playable = bool(video) and _finalise_orphan_video(Path(video))
        reason = _ORPHAN_REASON + (
            "; the video was salvaged" if playable else
            "; the video could not be finalised")
        with db.tx(root) as conn:
            conn.execute(
                "UPDATE playtest_session SET status = 'failed', "
                "processing_stage = 'orphaned', processing_error = ?, "
                "error = ?, ended_at = COALESCE(ended_at, datetime('now')), "
                # An unplayable file is worse than none: pt_video answers "no
                # video yet" instead of streaming bytes no player can decode.
                "video_path = CASE WHEN ? THEN video_path ELSE '' END "
                "WHERE id = ?",
                (reason, reason, 1 if playable else 0, session["id"]))
        try:
            activity.log(root, "playtest",
                         f"reaped orphaned recording {session['id']} "
                         f"({'video salvaged' if playable else 'video lost'})",
                         ref=str(session["id"]))
        except Exception:
            pass
        repaired.append({"session_id": session["id"], "killed": killed,
                         "video_playable": playable})
    return repaired


# ---------------------------------------------------------------------------
# Asset verification — expensive, so never on the poll thread
# ---------------------------------------------------------------------------
_verify_cache: dict[str, tuple[float, dict]] = {}
_verify_refreshing: set[str] = set()
# 10s was shorter than the interval between the two pollers that read it, so
# every /api/state paid for a full re-hash of every tracked binary.
_VERIFY_TTL = 30.0


def _asset_verification(root: Path, *, force: bool = False) -> dict:
    """Verify now, on this thread. The on-demand answer (POST /api/assets/verify)."""
    key = str(root.resolve())
    cached = _verify_cache.get(key)
    if not force and cached and time.monotonic() - cached[0] < _VERIFY_TTL:
        return cached[1]
    result = assets.verify(root)
    result["verified_at"] = time.time()
    result["stale"] = False
    _verify_cache[key] = (time.monotonic(), result)
    return result


def _verification_snapshot(root: Path) -> dict:
    """The verification /api/state gets — hashing never happens on the poll.

    assets.verify() full-sha256s every tracked binary; two independent pollers
    (3s and 4s) hitting a 10s cache meant a project of large .blend/.png files
    re-hashed itself forever, for a panel nobody was looking at. The first call
    computes (there is nothing else to show); after that the last answer is
    served immediately and a daemon thread refreshes it when it goes stale.
    """
    key = str(root.resolve())
    cached = _verify_cache.get(key)
    if cached is None:
        return _asset_verification(root)
    age = time.monotonic() - cached[0]
    if age >= _VERIFY_TTL and key not in _verify_refreshing:
        _verify_refreshing.add(key)

        def _refresh() -> None:
            try:
                _asset_verification(root, force=True)
            finally:
                _verify_refreshing.discard(key)

        threading.Thread(target=_refresh, name="asset-verify",
                         daemon=True).start()
    return {**cached[1], "stale": age >= _VERIFY_TTL}


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
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"}

# VIDEO GOES THROUGH THE SAME DOOR, and it did not, which quietly broke the one
# review step the cinematic seat treats as mandatory. The Takes panel renders a
# <video src="/api/preview?rel=...mp4"> for every generated shot; this endpoint
# answered 415 "images only", so the player drew as a black rectangle at 0:00.
# A seat whose brief says WATCH THE CLIP BEFORE YOU KEEP IT shipped with no way
# to watch one, and a shot that was already paid for looked like a broken file.
#
# .ogv is served for completeness, not for playback: Chromium removed Ogg Theora
# decoding in 123, so a kept cutscene — which this product transcodes to Theora
# precisely because it is the only thing GODOT will play — is undecodable in the
# browser reviewing it. The .mp4 candidate is what the panel should point at,
# and does; this list simply stops the endpoint lying about why.
_VIDEO_SUFFIXES = {".mp4", ".webm", ".ogv", ".mov", ".m4v"}
_PREVIEW_SUFFIXES = _IMAGE_SUFFIXES | _VIDEO_SUFFIXES
_VIDEO_MIME = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
               ".webm": "video/webm", ".ogv": "video/ogg"}
_RANGE_CHUNK = 1 << 18   # 256 KiB


def _ranged(target: Path, request: Optional[Request], mime: str):
    """Serve a file with byte-range support, because <video> requires it.

    A player does not simply GET a clip: it sends `Range: bytes=0-` and expects
    206 with a Content-Range. Starlette 0.38's FileResponse has no Range
    handling at all — it answers 200 with the whole file — and Chromium's
    response to that is a player that will not scrub and frequently will not
    start, which on screen is a black rectangle stuck at 0:00. That is
    indistinguishable from a broken encode, which is the wrong thing for a
    reviewer to conclude about a clip they have already paid for.

    Only the requested slice is read. A cutscene shot is tens of megabytes and
    the alternative is holding all of it in memory to hand back 256 KiB of it.
    """
    size = target.stat().st_size
    header = (request.headers.get("range") if request else "") or ""
    start, end = 0, size - 1
    partial = False

    match = re.match(r"bytes=(\d*)-(\d*)$", header.strip())
    if match and size:
        raw_start, raw_end = match.group(1), match.group(2)
        if raw_start:
            start = int(raw_start)
            end = int(raw_end) if raw_end else size - 1
        elif raw_end:
            # A suffix range: the LAST n bytes. Players use this to read the
            # moov atom of an mp4 that was written with it at the end.
            start = max(0, size - int(raw_end))
        if start >= size:
            return Response(status_code=416,
                            headers={"Content-Range": f"bytes */{size}"})
        end = min(end, size - 1)
        partial = True

    length = max(0, end - start + 1)

    def _chunks():
        with target.open("rb") as handle:
            handle.seek(start)
            left = length
            while left > 0:
                block = handle.read(min(_RANGE_CHUNK, left))
                if not block:
                    break
                left -= len(block)
                yield block

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length)}
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(_chunks(), status_code=206 if partial else 200,
                             media_type=mime, headers=headers)


def _root() -> Path:
    override = os.environ.get("BGATE_ROOT")
    if override:
        return Path(override)
    root = db.resolve_root()
    if root is None:
        # Last, below the cwd walk-up, so `bgate use` is a default and never an
        # override — see bgate_ui/deps.root() for the same order.
        root = project.active_root()
    if root is None:
        raise _api.unavailable("no .bgate project at or above the cwd — "
                               "run the dashboard from inside a game project, "
                               "or pick one with `bgate use <dir>`")
    return root


def _root_or_none() -> Optional[Path]:
    """The root, or None when there is no project yet.

    First run has to be reachable: the guard cannot demand a token from a
    directory that has no .bgate to keep one in, and /api/state has to be able
    to answer "no project" instead of 503-ing the whole page into an error card.
    """
    try:
        return _root()
    except (_api.ApiError, HTTPException):
        return None


# Same-origin + bearer-token guard on every mutation. Added after the COI
# middleware so it wraps it: a rejected request never reaches a handler.
_api.install_guard(app, _root)

# Streamer mode, OUTERMOST — added last, so in Starlette's ordering it wraps
# everything above it. That is load-bearing in both directions: it has to see
# the guard's own 401 body (which quotes the path it refused) on the way out,
# and it has to restore a substituted path on the way in before any handler
# tries to open it. Off unless BGATE_STREAMER says otherwise, and a no-op with
# no measurable cost when off.
_redact.install(app, _root_or_none)


@app.get("/api/streamer")
def streamer_status() -> dict:
    """Is the filter on, and what is it covering?

    Exists so the dashboard can SHOW it. A redactor that is quietly off looks
    exactly like one that is on and working, and the moment you find out is the
    moment it is too late — so the page gets a live answer rather than the user
    getting a promise. Reports counts, never values.
    """
    return _api.ok(_redact.status(_root_or_none()))


# ---------------------------------------------------------------------------
# Pagination, opt-in
# ---------------------------------------------------------------------------
# Every list endpoint here used to be unbounded, so a project past a few hundred
# rows quietly stopped showing its own data. They are all bounded now — but the
# dashboard and the seat modules read the BARE payload of these routes today
# ({items: [...]}, {artifacts: [...]}, ...), and flipping all of them to the
# {ok, data} envelope at once would blank the entire UI.
#
# So pagination is OPT-IN, keyed on whether the caller asked for it:
#   no ?limit / ?offset  -> the historical shape, bounded by that endpoint's
#                           legacy cap, with `page` added ALONGSIDE the existing
#                           key (added keys break nobody; moved keys break all).
#   ?limit or ?offset    -> the full envelope: {ok, data, page}.
# Callers migrate one at a time by starting to send ?limit. When the last one
# has, the legacy branch and this comment can go.

def _listing(request: Request, page: _api.Page, key: str, fetch,
             legacy_limit: int = _api.MAX_LIMIT) -> dict:
    """Run ``fetch(limit, offset) -> (items, total)`` under whichever mode the
    caller asked for. ``fetch`` takes the window so SQL-backed endpoints can push
    LIMIT/OFFSET down instead of materialising the table and slicing it."""
    explicit = "limit" in request.query_params or "offset" in request.query_params
    window = page if explicit else _api.Page(limit=legacy_limit, offset=0)
    items, total = fetch(window.limit, window.offset)
    envelope = window.envelope(items, total)
    if explicit:
        return envelope
    return {key: envelope["data"], "page": envelope["page"]}


def _sql_page(root: Path, source: str, order: str, params, limit: int,
              offset: int, decode=None) -> tuple[list[dict], int]:
    """One window of a table plus its true total. ``source`` is everything after
    FROM, WHERE clause included, so the COUNT counts exactly what the page pages."""
    conn = db.connect(root)
    total = conn.execute(
        f"SELECT count(*) FROM {source}", tuple(params)).fetchone()[0]
    window = _rows(conn.execute(
        f"SELECT * FROM {source} ORDER BY {order} LIMIT ? OFFSET ?",
        (*params, limit, offset)))
    return ([decode(row) for row in window] if decode else window), int(total)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    # The per-seat JS modules change often; a browser that caches them shows
    # stale code after an edit (this bit us with the art lightbox fix). Stamp
    # each module src with the newest seat-file mtime so the browser refetches
    # exactly when something changed, and caches otherwise.
    # CSS TOO. This stamped only .js, so every stylesheet edit sat in the
    # browser cache indefinitely — you fix a rule, reload, and see the old one,
    # which reads as "the fix did nothing" and sends you looking for a second
    # bug that is not there. Cost me an hour on an invisible dropdown.
    try:
        bust = str(int(max(p.stat().st_mtime
                           for p in (*_STATIC.rglob("*.js"),
                                     *_STATIC.rglob("*.css")))))
    except ValueError:
        bust = str(int(time.time()))
    html = re.sub(r'(/static/[\w/-]+\.(?:js|css))', r"\1?v=" + bust, html)
    return _inject_identity(_inject_token(_inject_settings(html)))


def _inject_settings(html: str) -> str:
    """Ride the client-side settings into the page as ``window.BGATE_SETTINGS``.

    Three registry keys are consumed by JS, not Python: the console's two poll
    intervals and the graph's phase cap. They were module constants, and putting
    them in the registry without delivering them made the Settings panel offer
    three fields that saved a value nothing ever read — which is worse than not
    listing them, because a switch that does nothing looks like a bug in the
    thing it was supposed to configure.

    In the page bootstrap rather than a second fetch on load, for the reason the
    cache-busting stamp above is: the browser already has to read this HTML, and a
    settings request on load is one more round trip before the first paint. Every
    consumer keeps its hardcoded fallback — a page served by an older build, or
    one whose project could not be read, must still poll at SOME rate.
    """
    try:
        from bgate_core import settings as _settings
        values = _settings.client(_root())
    except Exception:
        return html          # no project, no registry: the JS fallbacks stand
    if not values:
        return html
    shim = "<script>window.BGATE_SETTINGS=%s;</script>" % json.dumps(values)
    if "</head>" in html:
        return html.replace("</head>", shim + "</head>", 1)
    return shim + html


def _inject_identity(html: str) -> str:
    """Put WHICH PROJECT into the browser tab, not just into the page body.

    Three dashboards on one machine look identical, and one of them taking
    another's port after it stopped is a tab that is still open, still styled
    correctly, and now writing to a different game. Observed: settings changed
    in a tab that had silently become another project's. The tab title is the
    one piece of chrome a person reads without looking for it.
    """
    root = _root_or_none()
    if root is None:
        return html
    name = Path(root).name
    shim = ("<script>document.title=%s+' · builders gate';"
            "window.BGATE_PROJECT=%s;window.BGATE_ROOT=%s;</script>"
            % (json.dumps(name), json.dumps(name), json.dumps(str(root))))
    if "</head>" in html:
        return html.replace("</head>", shim + "</head>", 1)
    return shim + html


@app.get("/api/health")
def health() -> dict:
    """Which project this dashboard is serving. Unauthenticated on purpose.

    It exists so a SECOND dashboard can ask, before it binds, whether the port
    it wants is already answering for a different root — see serve(). That check
    is worthless if it needs a token, because the process asking has no way to
    hold another project's token, and the collision it prevents is the one that
    causes writes to land on the wrong game.

    Nothing sensitive: the root path and whether a board is up. No token, no
    settings, no work.
    """
    root = _root_or_none()
    return {"ok": True, "service": "builders-gate",
            "root": str(root) if root else "",
            "project": Path(root).name if root else ""}


def _inject_token(html: str) -> str:
    """Hand the page its dashboard token and make every fetch carry it.

    Patching window.fetch here rather than editing each of the ~200 call sites
    in the frontend: the token is an origin-wide requirement, and a wrapper is
    the only way to apply it without a diff nobody could review. Same-origin
    only — we never leak the token to a third-party URL.
    """
    root = _root_or_none()
    if root is None:
        return html  # no project yet: nothing to authenticate against
    try:
        token = _api.ensure_token(root)
    except Exception:
        return html
    shim = (
        "<script>window.BGATE_TOKEN=%r;(function(){const f=window.fetch;"
        "window.fetch=function(input,init){init=init||{};"
        "const url=typeof input==='string'?input:(input&&input.url)||'';"
        "const sameOrigin=!/^https?:\\/\\//i.test(url)||url.startsWith(location.origin);"
        "if(sameOrigin){const h=new Headers(init.headers||"
        "(typeof input==='object'&&input.headers)||{});"
        "h.set('X-Bgate-Token',window.BGATE_TOKEN);init.headers=h;}"
        "return f(input,init);};})();</script>"
    ) % token
    if "</head>" in html:
        return html.replace("</head>", shim + "</head>", 1)
    return shim + html


# Per-seat workspace JS modules live under static/ and load as /static/seats/*.js.
# StaticFiles is part of starlette (ships with FastAPI) — no new dependency.
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

# Per-feature endpoints (reference system, art-QA, godot workspace, etc.) live in
# bgate_ui/routes/*.py and register themselves here.
_registered_routes = _routes.register(app)


def _no_project(root: Optional[Path]) -> dict:
    """The first-run answer: 200, ``project: null``, and a sentence to act on.

    503-ing here was the audit's blocker in miniature — a fresh machine got an
    error card and nine empty nav items, because the page had nothing to render
    but the failure. The empty collections keep the shape the pollers already
    read, so nothing downstream has to learn a second payload.
    """
    return {
        "project": None,
        "root": None if root is None else str(root),
        "hint": "no Builders Gate project here yet — name one below, or run "
                "`bgate init <name>` in a terminal",
        "kinds": list(scaffold.KINDS),
        "known": project.known_projects(),
        "seats": [], "assets": [], "artifacts": [], "asset_groups": [],
        "sessions": [], "notes": [], "previews": [],
    }


@app.get("/api/state")
def state() -> dict:
    """Everything the dashboard shows, one poll — or the absence of a project."""
    root = _root_or_none()
    if root is None:
        return _no_project(None)

    try:
        proj = project.get(root)
    except LookupError:
        return _no_project(root)
    conn = db.connect(root)

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
        "verify": _verification_snapshot(root),
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
def activity_feed(request: Request, after_id: int = 0,
                  page: _api.Page = Depends()) -> dict:
    """The ticker. Poll with the last seen id for cheap incremental reads."""
    root = _root()
    return _listing(
        request, page, "events",
        lambda limit, offset: _sql_page(
            root, "activity WHERE id > ?", "id DESC", (after_id,),
            limit, offset),
        legacy_limit=60)


# What a peek will read as text, and how much of it. The rail is a rail: a
# 4000-line scene file scrolled inside a 400px panel is not a viewer, it is a
# denial of service on the reader — and the whole file is one "raw" click away.
PEEK_MAX_LINES = 400
PEEK_MAX_BYTES = 400_000
_AUDIO_SUFFIXES = {".wav", ".mp3", ".ogg", ".flac", ".m4a"}


def _peek_base(root: Path, item_id: int) -> Path:
    """Where a run's files live: its worktree if it has one, else the project.

    Shared by /api/peek and /api/preview so the two cannot disagree — they did,
    and the symptom was peek confidently reporting `kind: image` and then
    handing back a URL that 404'd.
    """
    if not item_id:
        return root
    try:
        worktree = str(_queue.get(root, int(item_id)).get("worktree") or "")
    except LookupError:
        worktree = ""
    if worktree and Path(worktree).is_dir():
        return Path(worktree).resolve()
    return root


@app.get("/api/peek")
def peek(rel: str, item_id: int = 0, offset: int = 0,
         lines: int = PEEK_MAX_LINES) -> dict:
    """Look at a file an agent is working on, without leaving the console.

    THE GAP: the live rail could name every file a run touched and open none of
    them. A path in a log line is not evidence — you cannot tell whether the
    scene the agent says it baked has anything in it, and the answer was a
    second editor window and a manual hunt.

    Answers metadata for anything and TEXT for what is text. Images and audio
    are named here but streamed by /api/preview and /api/audio/file, which
    already do byte ranges and content types properly. ``item_id`` looks inside
    that run's worktree when it has one, because a file an isolated agent is
    editing does not exist at the project root yet.
    """
    root = _root().resolve()
    base = _peek_base(root, item_id)

    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        # Same rule as /api/preview: the console is loopback-only, but a path
        # parameter that walks out of the project is still how a dashboard turns
        # into a file browser for the whole disk.
        raise _api.forbidden("path escapes the project root", rel=rel)

    out = {"rel": str(rel).replace("\\", "/"), "item_id": item_id,
           "worktree": "" if base == root else str(base)}
    if not target.is_file():
        out.update({"kind": "missing", "bytes": 0})
        return out

    stat = target.stat()
    suffix = target.suffix.lower()
    out.update({"bytes": stat.st_size, "mtime": stat.st_mtime,
                "name": target.name, "suffix": suffix})
    # Carry item_id into the URL. peek resolved the worktree to decide this file
    # EXISTS; a url that drops that context sends the browser to look for it
    # somewhere it was never going to be.
    scope = f"&item_id={item_id}" if item_id else ""
    if suffix in _IMAGE_SUFFIXES or suffix in {".gif"}:
        out["kind"] = "image"
        out["url"] = f"/api/preview?rel={quote(out['rel'])}{scope}"
        return out
    if suffix in _AUDIO_SUFFIXES:
        out["kind"] = "audio"
        out["url"] = f"/api/audio/file?rel={quote(out['rel'])}"
        return out
    if stat.st_size > PEEK_MAX_BYTES:
        out.update({"kind": "too_big", "lines": [],
                    "note": f"{stat.st_size:,} bytes — too large to read here"})
        return out

    try:
        text = target.read_text(encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, OSError):
        # Binary, or something we cannot read. Say which rather than rendering
        # mojibake and calling it a file.
        out.update({"kind": "binary", "lines": []})
        return out

    all_lines = text.splitlines()
    start = max(0, int(offset))
    count = max(1, min(int(lines or PEEK_MAX_LINES), PEEK_MAX_LINES))
    window = all_lines[start:start + count]
    out.update({
        "kind": "text",
        "lines": window,
        "first_line": start + 1,
        "lines_total": len(all_lines),
        "truncated": start + len(window) < len(all_lines),
    })
    return out


@app.get("/api/preview")
def preview(rel: str, item_id: int = 0,
            request: Request = None) -> FileResponse:
    """Serve one image OR video from inside the project, or a run's worktree.

    ``item_id`` resolves the same way /api/peek does, and for the same reason:
    a file an isolated agent is editing does not exist at the project root yet.
    Without it, every thumbnail of a worktree file 404'd and rendered as an
    empty bordered box — the console said the run was looking at something and
    then showed nothing, which reads as a broken preview rather than as a file
    that is simply somewhere else.
    """
    root = _root().resolve()
    base = _peek_base(root, item_id)
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise _api.forbidden("path escapes the project root", rel=rel)
    suffix = target.suffix.lower()
    if suffix not in _PREVIEW_SUFFIXES:
        raise _api.ApiError(
            415, "previewable images and video only",
            detail={"rel": rel, "allowed": sorted(_PREVIEW_SUFFIXES)})
    if not target.is_file():
        raise _api.not_found(f"nothing to preview at {rel}", rel=rel)
    if suffix not in _VIDEO_SUFFIXES:
        return FileResponse(target)
    return _ranged(target, request, _VIDEO_MIME.get(suffix, "video/mp4"))


# ---------------------------------------------------------------------------
# The queue + dispatch: orchestration lives here now
# ---------------------------------------------------------------------------
@app.get("/api/queue")
def queue_list(request: Request, status: Optional[str] = None,
               page: _api.Page = Depends()) -> dict:
    # NOTE: promoted playtest feedback does NOT auto-become work items. That
    # dumped raw transcript fragments ("[add] Jump velocity negative 172.A")
    # straight into the queue as dispatchable tasks -- garbage that spawned
    # agents on sentence fragments. The director SYNTHESIZES promoted feedback
    # into a few coherent work items (queue_add) instead; a fragment is not a task.
    root = _root()
    source = "work_item WHERE 1=1"
    params: list = []
    if status:
        source += " AND status = ?"
        params.append(status)
    # Same ordering queue.list_items uses — actionable first, then priority.
    # 'review' ranks with the live work: it is what a human owes an answer on,
    # and it is holding up everything chained behind it.
    order = ("CASE status WHEN 'queued' THEN 0 WHEN 'dispatched' THEN 1 "
             "WHEN 'review' THEN 2 ELSE 3 END, priority DESC, id")
    return _listing(
        request, page, "items",
        lambda limit, offset: _sql_page(
            root, source, order, params, limit, offset,
            decode=lambda row: _with_chain_state(root, row)))


def _with_chain_state(root: Path, row: dict) -> dict:
    """Tell the client WHY a queued row has no deploy button.

    A chained link that is not ready looks exactly like a normal queued item from
    the outside, so the UI happily offered a DEPLOY button that could only ever
    refuse. One extra lookup per row that actually has a predecessor; rows
    without one (the overwhelming majority) cost nothing.
    """
    row = dict(row)
    row["ready"] = True
    if not row.get("depends_on"):
        return row
    dep = db.connect(root).execute(
        "SELECT id, seat, title, status FROM work_item WHERE id = ?",
        (int(row["depends_on"]),)).fetchone()
    if dep is None:
        return row                       # predecessor deleted: no longer blocked
    if dep["status"] == "done":
        return row
    row["ready"] = False
    row["waiting_on"] = {"id": dep["id"], "seat": dep["seat"],
                         "title": dep["title"], "status": dep["status"]}
    return row


@app.post("/api/queue")
def queue_add(payload: dict) -> dict:
    """Add a work item.

    Two bugs in one line before: ``payload["seat"]`` on a body without one was a
    KeyError, i.e. a 500 for a typo; and source/source_ref were dropped on the
    floor, so anything filed through HTTP lost its provenance and the director's
    source badge rendered "manual" for playtest- and QA-born work alike.
    """
    seat = str(payload.get("seat") or "").strip()
    title = str(payload.get("title") or "").strip()
    missing = [field for field, value in (("seat", seat), ("title", title))
               if not value]
    if missing:
        raise _api.bad_request(f"a work item needs {' and '.join(missing)}",
                               missing=missing)
    try:
        priority = int(payload.get("priority") or 0)
    except (TypeError, ValueError):
        raise _api.bad_request("priority must be a whole number",
                               priority=payload.get("priority"))
    # A scope_tier_id was read off the payload and forwarded here, so the cut
    # line could refuse the item. Tiers are gone; a body that still carries the
    # field is simply ignored rather than 400'd, because an old bookmark or a
    # stale tab is not a malformed request.
    try:
        return _queue.add(_root(), seat, title,
                          brief=str(payload.get("brief") or ""),
                          priority=priority,
                          source=str(payload.get("source") or "manual"),
                          source_ref=str(payload.get("source_ref") or ""))
    except ValueError as exc:
        # Unknown seat and blank title — both the caller's, not the server's.
        raise _api.bad_request(str(exc), seat=seat)


@app.get("/api/screenmap")
def screenmap_view(fresh: int = 0) -> dict:
    """Atlas: the auto-derived graph of every screen and every asset it uses.

    Derived from the .tscn/.gd/.tres sources — no manifest — and cached for a
    few seconds, because this endpoint is polled by three panels at once and
    the derivation walks the whole project. `?fresh=1` forces a rescan; the
    write paths invalidate it outright, so the cache is never what you are
    looking at after your own edit."""
    from bgate_core import screenmap as _screenmap
    return _screenmap.scan_cached(_root(), force=bool(fresh))


# A waiter costs nothing while it waits (see queue_wait) but it still holds a
# connection, so it is bounded anyway: a caller that still cares re-issues, and
# the re-issue is also how we learn it is still alive.
_WAIT_MAX_S = 120
_WAIT_MAX_IDS = 200
_WAIT_TICK_S = 2.0


@app.get("/api/queue/wait")
async def queue_wait(ids: str, timeout_s: int = 60) -> dict:
    """LONG-POLL: block until any of the given items reaches done/failed.

    ids is comma-separated ("101,105"). This is the anti-sleep-poll: an
    orchestrator fires ONE background request per dispatch batch and gets
    woken the moment an agent self-reports (or dies and gets reaped) instead
    of guessing check-in intervals.

    Async, and capped at _WAIT_MAX_S. It used to be a sync handler sleeping up
    to THIRTY MINUTES, which pinned one of FastAPI's threadpool workers per
    waiter — a couple of dispatch batches and the server had no workers left for
    the dashboard's own polls, i.e. the feature starved the page that uses it.
    Awaiting on the event loop holds no worker at all; the 2s DB check runs in a
    thread only for the microseconds it takes.
    """
    import asyncio

    want = [int(x) for x in ids.split(",") if x.strip().isdigit()][:_WAIT_MAX_IDS]
    if not want:
        # Sentence + code, deliberately at 200: this route's callers are
        # orchestrators reading `error` as prose (see the contract test).
        return {"ok": False, "code": "bad_request",
                "error": "ids must be a comma-separated list of item ids"}
    budget = max(5, min(int(timeout_s), _WAIT_MAX_S))
    deadline = time.monotonic() + budget
    root = _root()

    def _statuses() -> dict:
        out = {}
        for item_id in want:
            try:
                out[item_id] = _queue.get(root, item_id)["status"]
            except LookupError:
                out[item_id] = "missing"
        return out

    while True:
        statuses = await asyncio.to_thread(_statuses)
        finished = {i: s for i, s in statuses.items()
                    if s in ("done", "failed", "cancelled", "missing")}
        remaining = deadline - time.monotonic()
        if finished or remaining <= 0:
            return {"finished": finished, "statuses": statuses,
                    "timed_out": not finished, "waited_budget_s": budget}
        await asyncio.sleep(min(_WAIT_TICK_S, remaining))


@app.post("/api/queue/{item_id}/dispatch")
def queue_dispatch(item_id: int, payload: Optional[dict] = None) -> dict:
    """Spawn an agent on a queued item.

    allow_dirty rides through to dispatch(), which otherwise refuses on an
    uncommitted tree. The route used to drop it, so the refusal was a dead end
    in the browser: the only ways past were an env var and a restart, and the
    message said "dispatch with allow_dirty" without there being any way to.
    None (the default) is not the same as False here — it means "unspecified",
    which lets BGATE_ALLOW_DIRTY still decide.
    """
    payload = payload or {}
    dirty = payload.get("allow_dirty")
    return _dispatch.dispatch(str(_root()), item_id,
                              model=payload.get("model") or None,
                              allow_dirty=None if dirty is None else bool(dirty))


@app.post("/api/queue/{item_id}/stop")
def queue_stop(item_id: int) -> dict:
    return _dispatch.stop(item_id)


@app.post("/api/queue/{item_id}/steer")
def queue_steer(item_id: int, payload: dict) -> dict:
    """Inject a live course-correction into a running agent (no restart)."""
    return _dispatch.steer(str(_root()), item_id, payload.get("text", ""))


@app.post("/api/queue/import-orbit")
def queue_import_orbit() -> dict:
    return _queue.import_orbit(_root())


@app.post("/api/queue/chain")
def queue_add_chain(payload: dict) -> dict:
    """File an ordered chain: link N does not start until link N-1 is done."""
    links = payload.get("links")
    if not isinstance(links, list) or not links:
        raise _api.bad_request("a chain needs a non-empty `links` array")
    try:
        rows = _queue.add_chain(_root(), [dict(x) for x in links],
                                chain_id=str(payload.get("chain_id") or ""),
                                source=str(payload.get("source") or "manual"))
    except (TypeError, ValueError) as exc:
        raise _api.bad_request(str(exc))
    return {"chain_id": rows[0]["chain_id"], "count": len(rows), "items": rows}


@app.get("/api/queue/chain/{chain_id}")
def queue_chain(chain_id: str) -> dict:
    items = _queue.chain(_root(), chain_id)
    if not items:
        raise _api.not_found(f"no chain {chain_id!r}")
    return {"chain_id": chain_id, "count": len(items), "items": items}


# ---------------------------------------------------------------------------
# The approval gate: none / agent / builder's. See bgate_core.gates.
# ---------------------------------------------------------------------------
@app.get("/api/gate")
def gate_state() -> dict:
    from bgate_core import gates as _gates
    return _gates.state(_root())


@app.post("/api/gate")
def gate_set(payload: dict) -> dict:
    from bgate_core import gates as _gates
    mode = str(payload.get("mode") or "").strip()
    try:
        return _gates.set_mode(_root(), mode)
    except ValueError as exc:
        raise _api.bad_request(str(exc), mode=mode, modes=list(_gates.MODES))


@app.get("/api/queue/review")
def queue_review() -> dict:
    """What the human owes an answer on. The drain list for the builder's gate."""
    items = _queue.awaiting_review(_root())
    return {"count": len(items), "items": items}


@app.post("/api/queue/{item_id}/approve")
def queue_approve(item_id: int, payload: Optional[dict] = None) -> dict:
    """Sign off held work: 'review' -> 'done', which releases the next link.

    Deliberately an HTTP route and not an MCP tool: the point of this gate is
    that a human clears it, and a tool an agent can call is a gate an agent can
    clear on its own behalf.
    """
    payload = payload or {}
    try:
        return _queue.approve(_root(), item_id,
                              note=str(payload.get("note") or ""))
    except LookupError as exc:
        raise _api.not_found(str(exc))
    except ValueError as exc:
        raise _api.bad_request(str(exc), item_id=item_id)


@app.post("/api/queue/{item_id}/reject")
def queue_reject(item_id: int, payload: dict) -> dict:
    """Send held work back with a reason — same fix path as a QA failure."""
    try:
        return _queue.reject(_root(), item_id,
                             reason=str((payload or {}).get("reason") or ""))
    except LookupError as exc:
        raise _api.not_found(str(exc))
    except ValueError as exc:
        raise _api.bad_request(str(exc), item_id=item_id)


@app.get("/api/agents")
def agents(request: Request, page: _api.Page = Depends()) -> dict:
    # In-memory process table, not SQL — there is nothing to push a LIMIT into.
    running = _dispatch.status(str(_root()))
    return _listing(
        request, page, "agents",
        lambda limit, offset: (running[offset:offset + limit], len(running)))


_RUN_MARKER = '"bgate_run_start"'


@app.get("/api/agent-log/{item_id}")
def agent_log(item_id: int, tail: int = 60, all_runs: bool = False) -> dict:
    """The raw stream-json log, one RUN at a time.

    The log appends across re-dispatches, so a tail that spanned a boundary
    interleaved a dead run's output with the live one and read as a single
    incoherent session — read_activity has always seeked past the last
    bgate_run_start marker, this did not. ?all_runs=1 keeps the history, with a
    visible separator where a splice used to be silent.
    """
    path = _root() / ".bgate" / "agents" / f"item-{item_id}.log"
    if not path.is_file():
        return {"lines": [], "runs": 0, "run": 0}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    starts = [i for i, line in enumerate(lines) if _RUN_MARKER in line]
    runs = len(starts)
    if all_runs:
        out: list[str] = []
        for index, line in enumerate(lines):
            if index in starts:
                out.append(f"───── run {starts.index(index) + 1} of {runs} ─────")
            out.append(line)
        return {"lines": out[-tail:], "runs": runs, "run": runs}
    if starts:
        lines = lines[starts[-1]:]
    return {"lines": lines[-tail:], "runs": runs, "run": runs}


@app.get("/api/agent-activity/{item_id}")
def agent_activity(item_id: int) -> dict:
    """Readable live feed of what a dispatched agent is doing — parsed from its
    stream-json log into tool calls, messages, and the final result."""
    return _dispatch.read_activity(str(_root()), item_id)


@app.get("/api/artifacts")
def artifact_list(request: Request, status: Optional[str] = None,
                  logical_name: Optional[str] = None,
                  page: _api.Page = Depends()) -> dict:
    root = _root()
    if status and status not in artifacts.STATUSES:
        raise _api.bad_request(f"status must be one of {artifacts.STATUSES}",
                               status=status)
    source = "artifact_revision WHERE 1=1"
    params: list = []
    if logical_name:
        source += " AND logical_name = ?"
        params.append(logical_name)
    if status:
        source += " AND status = ?"
        params.append(status)
    return _listing(
        request, page, "artifacts",
        lambda limit, offset: _sql_page(
            root, source, "created_at DESC, id DESC", params, limit, offset,
            # The JSON columns need the same unpacking list_revisions does.
            decode=artifacts._decode),
        legacy_limit=100)


@app.post("/api/artifacts/{artifact_id}/review")
def artifact_review(artifact_id: int, payload: dict) -> dict:
    try:
        return artifacts.review(
            _root(), artifact_id, payload.get("status", ""), payload.get("note", ""))
    except (LookupError, ValueError) as exc:
        raise _api.bad_request(str(exc), artifact_id=artifact_id)


@app.post("/api/artifacts/{artifact_id}/react")
def artifact_react(artifact_id: int, payload: dict) -> dict:
    """Like/dislike a produced artifact and fan the feedback out three ways:
      1. disposition — like -> approved, dislike -> rejected (with the note);
      2. durable art-seat preference note (future agents read it in seat_brief);
      3. if a live agent is working the item, steer it to course-correct NOW.
    Payload: {verdict: 'like'|'dislike', note?: str, item_id?: int}.

    Every effect reports its own outcome in ``effects``, and ``ok`` is true only
    when the ones that were attempted all worked. It used to return ok:true
    unconditionally with a grab-bag of optional *_error keys, so a verdict that
    saved nothing and steered nobody looked exactly like one that did all three.
    """
    root = _root()
    verdict = (payload.get("verdict") or "").lower()
    note = (payload.get("note") or "").strip()
    item_id = payload.get("item_id")
    if verdict not in ("like", "dislike"):
        raise _api.bad_request("verdict must be 'like' or 'dislike'",
                               verdict=payload.get("verdict"))
    try:
        art = artifacts.get(root, artifact_id)
    except LookupError as exc:
        raise _api.not_found(str(exc), artifact_id=artifact_id)
    name = art.get("logical_name") or f"artifact {artifact_id}"
    effects: dict[str, dict] = {}
    out = {"ok": True, "verdict": verdict, "artifact": name, "effects": effects}

    def _effect(kind: str, work) -> None:
        """Run one fan-out leg and record what actually happened to it."""
        try:
            effects[kind] = {"attempted": True, "ok": True, **(work() or {})}
        except Exception as exc:
            effects[kind] = {"attempted": True, "ok": False,
                             "error": f"{type(exc).__name__}: {exc}"}

    # 1. disposition
    status = "approved" if verdict == "like" else "rejected"

    def _review() -> dict:
        artifacts.review(root, artifact_id, status, note)
        out["reviewed"] = status          # legacy key the dashboard reads
        return {"status": status}

    _effect("disposition", _review)

    # 2. durable preference the next art agent reads in seat_brief
    body = (("KEEP / on-model" if verdict == "like" else "AVOID / off-model")
            + f" — {name}" + (f": {note}" if note else "")
            + " (via live like/dislike).")

    def _note() -> dict:
        seats.post_note(root, "art", "ART PREFERENCE — " + body,
                        topic="art-feedback")
        out["saved_preference"] = True
        return {}

    _effect("seat_note", _note)

    # 3. live course-correction — dislike always steers; a like only steers if it
    #    carries a note (a bare like is just a keeper, no need to interrupt).
    wants_steer = bool(item_id) and (verdict == "dislike" or bool(note))
    if not wants_steer:
        effects["steer"] = {"attempted": False, "ok": True,
                            "reason": "no running item to steer" if not item_id
                                      else "a bare like does not interrupt work"}
    else:
        icon = "👎 disliked" if verdict == "dislike" else "👍 liked"
        msg = (f"DIRECTOR FEEDBACK on {name}: {icon}."
               + (f" {note}." if note else "")
               + (" Regenerate that animation to fix it (re-run image_sprites for it);"
                  " do NOT self-approve." if verdict == "dislike" else ""))

        def _steer() -> dict:
            result = _dispatch.steer(str(root), int(item_id), msg)
            out["steer"] = result          # legacy keys the dashboard reads
            out["steered"] = bool(result.get("ok"))
            # dispatch.steer answers in sentence+code form, not by raising: a
            # steer aimed at an item with no live agent is a real failure here.
            return {"ok": bool(result.get("ok")),
                    "error": str(result.get("error") or "")}

        _effect("steer", _steer)
        out.setdefault("steered", False)

    out["failed"] = sorted(k for k, e in effects.items()
                           if e["attempted"] and not e["ok"])
    out["ok"] = not out["failed"]
    return out


@app.post("/api/artifacts/{artifact_id}/restore")
def artifact_restore(artifact_id: int) -> dict:
    """Make an older revision current again. Generations overwrite the stable
    sheet path, so old versions live only in the per-revision archive; this copies
    that archived render back over the live sheet file the game reads."""
    import shutil
    from bgate_core import activity
    root = _root()
    try:
        art = artifacts.get(root, artifact_id)
    except LookupError as exc:
        raise _api.not_found(str(exc), artifact_id=artifact_id)
    arch = (art.get("metadata") or {}).get("preview")
    if not arch or not Path(arch).is_file():
        raise _api.bad_request("no archived snapshot for this revision to restore",
                               artifact_id=artifact_id)
    dst = Path(root) / art["path"]
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(arch, dst)
    activity.log(root, "artifact",
                 f"restored r{art['revision']} of {art['logical_name']} -> {art['path']}",
                 ref=str(artifact_id))
    return {"ok": True, "restored_revision": art["revision"],
            "logical_name": art["logical_name"], "path": art["path"]}


@app.get("/api/assets/workspace")
def asset_workspace(request: Request, page: _api.Page = Depends()) -> dict:
    # workspace() folds revisions into logical groups in Python, so the window
    # is applied to the folded result rather than to the underlying rows.
    groups = artifacts.workspace(_root())
    return _listing(
        request, page, "groups",
        lambda limit, offset: (groups[offset:offset + limit], len(groups)))


@app.post("/api/artifacts/{artifact_id}/regenerate")
def artifact_regenerate(artifact_id: int, payload: Optional[dict] = None) -> dict:
    try:
        return artifacts.regenerate(
            _root(), artifact_id, (payload or {}).get("reason", ""))
    except (LookupError, ValueError) as exc:
        raise _api.bad_request(str(exc), artifact_id=artifact_id)


@app.post("/api/artifacts/{artifact_id}/feedback/{item_id}")
def artifact_link_feedback(artifact_id: int, item_id: int,
                           payload: Optional[dict] = None) -> dict:
    try:
        return artifacts.link_feedback(
            _root(), artifact_id, item_id,
            float((payload or {}).get("confidence", 1.0)))
    except (LookupError, ValueError) as exc:
        raise _api.bad_request(str(exc), artifact_id=artifact_id, item_id=item_id)


# /api/iterations and /api/iterations/{id} used to live here, and `iterations`
# used to be a key on /api/state. The Timeline view that read them is gone; the
# state key was costing a hydrating query on EVERY dashboard poll for data
# nothing rendered. The MCP `iteration_status` tool is the surviving read path.


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
        # Sentence + code at 200, like dispatch(): the record button renders
        # `error` as prose next to a still-usable control, and a preflight that
        # says "your mic is muted" is advice, not a transport failure.
        return {"ok": False, "code": "record_failed",
                "error": f"{type(exc).__name__}: {exc}"}


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
        # Sentence + code at 200 — same convention as pt_start above.
        return {"ok": False, "code": "not_recording", "error": str(exc)}
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
            # The session clock's ZERO. The notepad needs it to show what
            # mm:ss a note is about to land on before it posts — the server
            # does the real conversion, but a pad that cannot say "02:14"
            # until after you save is asking you to trust it blind.
            "started_epoch": recording["started_epoch"],
        }
        # Mic level rides along on the poll the record button already makes —
        # a dead mic has to be visible while the playthrough can still be saved,
        # not discovered at transcription time when it is gone.
        try:
            recording_state["level"] = playtest.live_level(root, recording["id"])
        except Exception:
            recording_state["level"] = None
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
        raise _api.conflict("session processing is already running",
                            session_id=session_id)
    session = playtest.get(root, session_id)
    if not session["audio_path"] or not Path(session["audio_path"]).is_file():
        raise _api.conflict("session has no captured audio to transcribe",
                            session_id=session_id)
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
        raise _api.conflict(str(exc), session_id=session_id)


@app.get("/api/playtest/{session_id}")
def pt_review(session_id: int, request: Request,
              page: _api.Page = Depends()) -> dict:
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
        # The one list here that can run long is the feedback items. This is a
        # detail view, not a list route — replacing the whole body with a bare
        # list would drop session/transcript/asset_options — so the window is
        # applied in place and described by `page`, in both modes.
        found = result["items"]
        windowed = _listing(request, page, "items",
                            lambda limit, offset: (found[offset:offset + limit],
                                                   len(found)))
        result["items"] = windowed.get("items", windowed.get("data"))
        result["page"] = windowed["page"]
        return result
    except LookupError as exc:
        raise _api.not_found(str(exc), session_id=session_id)


_RANGE_RE = re.compile(r"^bytes\s*=\s*(\d*)\s*-\s*(\d*)$")
# 512 KiB: a seek should cost one read, and a session recording is hundreds of
# megabytes — it never gets read into memory whole.
_VIDEO_CHUNK = 512 * 1024


def _parse_range(header: str, size: int):
    """``(start, end)`` inclusive, ``None`` to serve the whole file, or the
    string ``"unsatisfiable"``.

    A unit we do not speak is ignored per RFC 7233 (fall through to a 200); a
    *bytes* range we cannot honour is an error the player must see, or it silently
    renders a seek as "no video".
    """
    header = (header or "").strip()
    if not header:
        return None
    if not header.lower().startswith("bytes"):
        return None
    match = _RANGE_RE.match(header)
    if not match:
        return "unsatisfiable"  # malformed, or a multi-range we do not serve
    first, last = match.group(1), match.group(2)
    if not first and not last:
        return "unsatisfiable"
    if not first:
        # Suffix form (bytes=-N): the final N bytes. Players use it to read the
        # moov atom of a file whose index sits at the end.
        wanted = int(last)
        if wanted <= 0:
            return "unsatisfiable"
        return max(0, size - wanted), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or end < start:
        return "unsatisfiable"
    return start, min(end, size - 1)


def _file_window(path: Path, start: int, end: int):
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = handle.read(min(_VIDEO_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@app.get("/api/playtest/{session_id}/video")
def pt_video(session_id: int, request: Request) -> Response:
    """Serve the session recording with byte ranges.

    Every timeline marker, moment dot and transcript line in the review overlay
    is a seek, and a seek is a Range request. Without 206 support the browser
    can only play from zero, so the whole review UI is a promise the transport
    cannot keep.
    """
    root = _root().resolve()
    try:
        session = playtest.get(root, session_id)
    except LookupError:
        raise _api.not_found(f"no playtest session {session_id}",
                             session_id=session_id)

    raw = (session.get("video_path") or "").strip()
    stage = (session.get("processing_stage") or session.get("status") or "").strip()
    # A session with no recording is not a security event. It said "path escapes
    # the project root" — alarming, and wrong: Path("") resolves to the cwd.
    if not raw:
        raise _api.not_found("this session has no video yet",
                             session_id=session_id, stage=stage)

    path = Path(raw).resolve()
    try:
        path.relative_to(root / ".bgate" / "playtests")
    except ValueError:
        raise _api.ApiError(403, "video path escapes playtest storage",
                            code="forbidden")
    if not path.is_file():
        raise _api.not_found("this session has no video yet",
                             session_id=session_id, stage=stage)

    size = path.stat().st_size
    headers = {"Accept-Ranges": "bytes"}
    span = _parse_range(request.headers.get("range", ""), size)

    if span == "unsatisfiable":
        return JSONResponse(
            status_code=416,
            content=_api.error_body(416, "requested range not satisfiable",
                                    code="range_not_satisfiable",
                                    detail={"size": size}),
            headers={**headers, "Content-Range": f"bytes */{size}"})

    if span is None:
        if request.headers.get("range"):
            # We have DELIBERATELY ignored this Range: it names a unit we do not
            # speak, and RFC 7233 says that is a 200 with the whole body, not an
            # error. Starlette 1.0's FileResponse grew its own Range support and
            # answers 400 for a header it cannot parse — so handing it a request
            # still carrying `Range: frames=1-2` turns our deliberate 200 into a
            # client error. Serve the body here so exactly one layer reads Range.
            return StreamingResponse(
                _file_window(path, 0, size - 1), media_type="video/mp4",
                headers={**headers, "Content-Length": str(size)})
        return FileResponse(path, media_type="video/mp4", headers=headers)

    start, end = span
    return StreamingResponse(
        _file_window(path, start, end), status_code=206, media_type="video/mp4",
        headers={**headers,
                 "Content-Range": f"bytes {start}-{end}/{size}",
                 "Content-Length": str(end - start + 1)})


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
        raise _api.bad_request(str(exc), item_id=item_id)


@app.post("/api/playtest/items/{item_id}/dismiss")
def pt_dismiss(item_id: int) -> dict:
    try:
        return playtest.dismiss(_root(), item_id)
    except LookupError as exc:
        raise _api.not_found(str(exc), item_id=item_id)


@app.post("/api/playtest/items/{item_id}/merge")
def pt_merge(item_id: int, payload: dict) -> dict:
    try:
        return playtest.merge(_root(), item_id, int(payload["target_id"]))
    except KeyError:
        raise _api.bad_request("merge needs a target_id", item_id=item_id)
    except (LookupError, ValueError) as exc:
        raise _api.bad_request(str(exc), item_id=item_id)


# ---------------------------------------------------------------------------
# Play the game inside the app — always the CURRENT build
# ---------------------------------------------------------------------------
@app.get("/api/play/status")
def play_status() -> dict:
    from bgate_ui import webbuild
    root = _root()
    status = webbuild.status(root)
    # The controls the player is about to use, read from the project's own
    # input map. The panel used to hardcode one game's fighting-game bindings
    # for every project; anything it says now is something the game does.
    status["controls"] = _controls.for_project(root)
    return status


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
        raise _api.not_found("no web build — export it first (tech seat)")
    target = (web / (file_path or "index.html")).resolve()
    try:
        target.relative_to(web)
    except ValueError:
        raise _api.forbidden("path escapes the build dir", path=file_path)
    if not target.is_file():
        raise _api.not_found(f"no file {file_path} in the web build",
                             path=file_path)
    return FileResponse(target)


def _serving_elsewhere(port: int, root) -> str:
    """The root another dashboard is already serving on this port, or "".

    Short timeout and every failure means "nothing there": the probe exists to
    catch a specific confusing collision, and it must never be the reason the
    dashboard will not start. A non-Builders-Gate service on the port answers
    nothing we recognise and is left to uvicorn's own bind error.
    """
    import json as _json
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/health",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=0.4) as resp:
            body = _json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except Exception:
        return ""
    theirs = str((body or {}).get("root") or "").strip()
    if not theirs:
        return ""
    try:
        same = root is not None and Path(theirs).resolve() == Path(root).resolve()
    except Exception:
        same = str(theirs) == str(root)
    return "" if same else theirs


def serve(port: int = 7788) -> None:
    """Run the dashboard, and SAY WHERE IT IS.

    `python -m bgate_ui` printed literally nothing — no URL, no port, no project
    — so the command that starts the product looked like a hang. uvicorn's own
    banner is suppressed (log_level=warning) on purpose; this replaces it with
    the two facts a person actually needs.
    """
    import uvicorn

    url = f"http://127.0.0.1:{port}"
    root = _root_or_none()

    # IS SOMEBODY ELSE ALREADY SERVING THIS PORT, FOR A DIFFERENT GAME?
    #
    # OBSERVED, AND IT CAUSED WRONG-PROJECT WRITES. Three sessions on one
    # machine each started a dashboard; two of them hardcoded a different
    # BGATE_ROOT in a scratch runner. When the first server stopped, another
    # took 7788. The browser tab stayed open and looked IDENTICAL — same layout,
    # same port, same styling — and settings changes made in it were landing on
    # a different game. It was caught only by noticing that max_concurrent read
    # 11 when a human had set it to 4.
    #
    # Refusing to bind is not possible from here (uvicorn owns the socket), but
    # asking first is, and it costs a 400 ms probe on startup.
    other = _serving_elsewhere(port, root)
    if other:
        print(f"builders gate · REFUSING to start on {url}")
        print("  another dashboard is already serving that port, for a "
              "DIFFERENT project:")
        print(f"    it is serving : {other}")
        print(f"    you asked for : {root or '(no project here)'}")
        print("  Two dashboards on one port means a browser tab that looks "
              "right and writes to the wrong game.")
        print(f"  Stop that one, or start this on another port: "
              f"bgate serve --port {port + 1}")
        raise SystemExit(2)

    print(f"builders gate · dashboard on {url}")
    if root is None:
        print("  no project here yet — open the URL and create one, "
              "or run: bgate init <name>")
    else:
        print(f"  project: {root}")
    print("  ctrl-c to stop")

    # 127.0.0.1 on purpose: this is a local window into a local store.
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
