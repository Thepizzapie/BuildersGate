"""The dashboard backend — the local cockpit over the project's SQLite store.

One page, polled JSON, no build step, no node, no CDN. Mutations are deliberately
limited to user-facing orchestration and review: queue/dispatch, recording,
feedback disposition, and generated-artifact approval.

Run: bgate serve [--port 7788]   (from anywhere inside a project, or BGATE_ROOT)
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from bgate_core import (
    activity, artifacts, assets, bible, db, iterations, lore, playtest,
    project, scaffold, seats,
)
from bgate_core import queue as _queue
from bgate_core.util import rows as _rows
from bgate_ui import api as _api
from bgate_ui import dispatch as _dispatch
from bgate_ui import qa_gate as _qa_gate
from bgate_ui import routes as _routes

app = FastAPI(title="builders-gate-ui", docs_url=None, redoc_url=None)

# One error envelope for every failure — see bgate_ui/api.py. Installed before
# anything else so even a startup-time raise comes back as parseable JSON.
_api.install_error_handlers(app)


@app.on_event("startup")
def _start_qa_gate() -> None:
    # Auto-QA gate: agent-completed maker items get a nit-picky QA review
    # before they count. Fail-safe — a missing project just means no gate.
    try:
        _qa_gate.start(str(_root()))
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


def _root_or_none() -> Optional[Path]:
    """The root, or None when there is no project yet.

    First run has to be reachable: the guard cannot demand a token from a
    directory that has no .bgate to keep one in, and /api/state has to be able
    to answer "no project" instead of 503-ing the whole page into an error card.
    """
    try:
        return _root()
    except HTTPException:
        return None


# Same-origin + bearer-token guard on every mutation. Added after the COI
# middleware so it wraps it: a rejected request never reaches a handler.
_api.install_guard(app, _root)


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
    try:
        bust = str(int(max(p.stat().st_mtime
                           for p in _STATIC.rglob("*.js"))))
    except ValueError:
        bust = str(int(time.time()))
    html = re.sub(r'(/static/[\w/-]+\.js)', r"\1?v=" + bust, html)
    return _inject_token(html)


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
        "iterations": [], "sessions": [], "notes": [], "previews": [],
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
    order = ("CASE status WHEN 'queued' THEN 0 WHEN 'dispatched' THEN 1 "
             "ELSE 2 END, priority DESC, id")
    return _listing(
        request, page, "items",
        lambda limit, offset: _sql_page(
            root, source, order, params, limit, offset))


@app.post("/api/queue")
def queue_add(payload: dict) -> dict:
    return _queue.add(_root(), payload["seat"], payload["title"],
                      brief=payload.get("brief", ""),
                      priority=int(payload.get("priority", 0)))


@app.get("/api/screenmap")
def screenmap_view() -> dict:
    """Atlas: the auto-derived graph of every screen and every asset it uses.
    Derived fresh per call from the .tscn/.gd/.tres sources — no manifest."""
    from bgate_core import screenmap as _screenmap
    return _screenmap.scan(_root())


@app.get("/api/queue/wait")
def queue_wait(ids: str, timeout_s: int = 600) -> dict:
    """LONG-POLL: block until any of the given items reaches done/failed.

    ids is comma-separated ("101,105"). This is the anti-sleep-poll: an
    orchestrator fires ONE background request per dispatch batch and gets
    woken the moment an agent self-reports (or dies and gets reaped) instead
    of guessing check-in intervals. Runs in FastAPI's threadpool, so blocking
    here doesn't stall the event loop; check interval is 2s against the DB,
    which every completion path writes through (queue.set_status).
    """
    import time as _time
    want = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    if not want:
        return {"error": "ids must be a comma-separated list of item ids"}
    deadline = _time.monotonic() + max(5, min(int(timeout_s), 1800))
    root = _root()
    while True:
        statuses = {}
        for i in want:
            try:
                statuses[i] = _queue.get(root, i)["status"]
            except LookupError:
                statuses[i] = "missing"
        finished = {i: s for i, s in statuses.items()
                    if s in ("done", "failed", "missing")}
        if finished or _time.monotonic() >= deadline:
            return {"finished": finished, "statuses": statuses,
                    "timed_out": not finished}
        _time.sleep(2.0)


@app.post("/api/queue/{item_id}/dispatch")
def queue_dispatch(item_id: int, payload: Optional[dict] = None) -> dict:
    payload = payload or {}
    return _dispatch.dispatch(str(_root()), item_id,
                              model=payload.get("model") or None)


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


@app.get("/api/agents")
def agents(request: Request, page: _api.Page = Depends()) -> dict:
    # In-memory process table, not SQL — there is nothing to push a LIMIT into.
    running = _dispatch.status(str(_root()))
    return _listing(
        request, page, "agents",
        lambda limit, offset: (running[offset:offset + limit], len(running)))


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
        raise HTTPException(400, str(exc))


@app.post("/api/artifacts/{artifact_id}/react")
def artifact_react(artifact_id: int, payload: dict) -> dict:
    """Like/dislike a produced artifact and fan the feedback out three ways:
      1. disposition — like -> approved, dislike -> rejected (with the note);
      2. durable art-seat preference note (future agents read it in seat_brief);
      3. if a live agent is working the item, steer it to course-correct NOW.
    Payload: {verdict: 'like'|'dislike', note?: str, item_id?: int}."""
    root = _root()
    verdict = (payload.get("verdict") or "").lower()
    note = (payload.get("note") or "").strip()
    item_id = payload.get("item_id")
    try:
        art = artifacts.get(root, artifact_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    name = art.get("logical_name") or f"artifact {artifact_id}"
    out = {"ok": True, "verdict": verdict, "artifact": name}

    # 1. disposition
    status = {"like": "approved", "dislike": "rejected"}.get(verdict, "")
    if status:
        try:
            artifacts.review(root, artifact_id, status, note); out["reviewed"] = status
        except Exception as exc:
            out["review_error"] = str(exc)

    # 2. durable preference the next art agent reads in seat_brief
    if verdict:
        body = (("KEEP / on-model" if verdict == "like" else "AVOID / off-model")
                + f" — {name}" + (f": {note}" if note else "")
                + f" (via live like/dislike).")
        try:
            seats.post_note(root, "art", "ART PREFERENCE — " + body, topic="art-feedback")
            out["saved_preference"] = True
        except Exception as exc:
            out["note_error"] = str(exc)

    # 3. live course-correction — dislike always steers; a like only steers if it
    #    carries a note (a bare like is just a keeper, no need to interrupt).
    if item_id and (verdict == "dislike" or note):
        icon = "👎 disliked" if verdict == "dislike" else "👍 liked"
        msg = (f"DIRECTOR FEEDBACK on {name}: {icon}."
               + (f" {note}." if note else "")
               + (" Regenerate that animation to fix it (re-run image_sprites for it);"
                  " do NOT self-approve." if verdict == "dislike" else ""))
        try:
            r = _dispatch.steer(str(root), int(item_id), msg)
            out["steered"] = bool(r.get("ok")); out["steer"] = r
        except Exception as exc:
            out["steer_error"] = str(exc)
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
        raise HTTPException(404, str(exc))
    arch = (art.get("metadata") or {}).get("preview")
    if not arch or not Path(arch).is_file():
        raise HTTPException(400, "no archived snapshot for this revision to restore")
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
def iteration_list(request: Request, page: _api.Page = Depends()) -> dict:
    root = _root()

    def fetch(limit: int, offset: int) -> tuple[list[dict], int]:
        conn = db.connect(root)
        total = conn.execute("SELECT count(*) FROM iteration").fetchone()[0]
        # Page the ids, then hydrate only that window — iterations.get pulls
        # events and checks per row, which is far too expensive to do table-wide.
        ids = [int(row[0]) for row in conn.execute(
            "SELECT id FROM iteration ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset))]
        return [iterations.get(root, i) for i in ids], int(total)

    return _listing(request, page, "iterations", fetch, legacy_limit=30)


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
        raise HTTPException(404, str(exc))


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
    """Run the dashboard, and SAY WHERE IT IS.

    `python -m bgate_ui` printed literally nothing — no URL, no port, no project
    — so the command that starts the product looked like a hang. uvicorn's own
    banner is suppressed (log_level=warning) on purpose; this replaces it with
    the two facts a person actually needs.
    """
    import uvicorn

    url = f"http://127.0.0.1:{port}"
    root = _root_or_none()
    print(f"builders gate · dashboard on {url}")
    if root is None:
        print("  no project here yet — open the URL and create one, "
              "or run: bgate init <name>")
    else:
        print(f"  project: {root}")
    print("  ctrl-c to stop")

    # 127.0.0.1 on purpose: this is a local window into a local store.
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
