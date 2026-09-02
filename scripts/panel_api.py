"""The bridge a Pulsiron panel button crosses to reach `bgate serve`.

A panel control is a value; a panel button is a ROUTINE, and a routine is a
shell command. So something has to turn "prompt=a cracked office mug, kind=prop"
into an HTTP POST and turn the answer back into a few lines a human can read in
the run-output pane. Doing that with curl means embedding JSON in a Windows
command line — one apostrophe in a prompt and the request is malformed in a way
the panel reports as a network error. This takes plain `--flag value` arguments
instead, builds the JSON here, and prints a SHORT human summary rather than the
raw payload, because run-output is a pane, not a terminal.

Nothing here is bgate-specific beyond the paths: it talks to 127.0.0.1:7788,
the dashboard's own loopback API, so a panel can only do what the dashboard can
already do, and only while `bgate serve` is up.

    python tools/panel_api.py art --prompt "..." --filename mug.png --kind prop
    python tools/panel_api.py music --prompt "..." --name tension
    python tools/panel_api.py queue --seat art --title "..." --brief "..."
    python tools/panel_api.py board
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

# The dashboard's own loopback address. Overridable because `bgate serve` takes
# --port and `bgate app` takes whatever port the OS handed it, and a panel that
# hardcodes 7788 is a panel that only works on the default.
BASE = os.environ.get("BGATE_UI_URL", "http://127.0.0.1:7788").rstrip("/")

# A PANEL'S PTY IS NOT A CONSOLE. Python picks the locale encoding for a pipe,
# which on Windows is cp1252 — and every refusal message here carries an em
# dash. A run that dies encoding its own error message reports `exit 1` with an
# empty log, which is the least debuggable thing this tool can do.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass  # already wrapped, or not reconfigurable — printing still works


def refuse(message: str) -> int:
    """Say why nothing happened, on the channel a failed step actually shows.

    Panels render the step's error box from stderr; a refusal written to stdout
    came back as a bare `exit 1` with nothing above it. Anything that returns
    non-zero should go through here.
    """
    print(message, file=sys.stderr, flush=True)
    return 1
# A generation POST answers 202 immediately (it starts a job); the wait below is
# what turns that into "the picture is at X" for someone watching a panel.
POLL_EVERY_S = 3.0
POLL_MAX_S = 600.0


class Down(RuntimeError):
    """The dashboard is not answering — the single most common failure here,
    and worth its own message: a panel button that silently does nothing is
    indistinguishable from a broken panel."""


# The dashboard refuses an unauthenticated MUTATION (GETs are exempt): a page
# in any browser can POST to loopback, so the guard wants a token only something
# with read access to the project's .bgate/ could know. This CLI is exactly
# that — it runs on the machine, as the user — so it reads the token off disk
# rather than asking anyone to paste one into a panel field.
_TOKEN: str | None = None


def token() -> str:
    """The running dashboard's token, found through the dashboard itself.

    /api/project is a safe method and answers unauthenticated; it names the
    project root, and the token sits at <root>/.bgate/ui-token. Cached because
    every verb here makes at least two calls.
    """
    global _TOKEN
    if _TOKEN is not None:
        return _TOKEN
    _TOKEN = ""
    try:
        data = (call("GET", "/api/project") or {}).get("data") or {}
        root = data.get("root") or data.get("cwd") or ""
        if root:
            spot = pathlib.Path(root) / ".bgate" / "ui-token"
            _TOKEN = spot.read_text(encoding="utf-8").strip()
    except (OSError, Down, SystemExit):
        # No token is not fatal: BGATE_NO_AUTH runs without one, and a read
        # failure should surface as the API's own 401 rather than as a crash
        # here with no mention of which project was being served.
        _TOKEN = ""
    return _TOKEN


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if method not in ("GET", "HEAD"):
        got = token()
        if got:
            headers["X-BGate-Token"] = got
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as answer:
            raw = answer.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except (json.JSONDecodeError, AttributeError):
            pass
        raise SystemExit(f"{exc.code} {path}: {detail}")
    except urllib.error.URLError as exc:
        raise Down(
            f"no dashboard on {BASE} ({exc.reason}) — run `bgate serve` "
            "in the project, then press this button again") from exc
    return json.loads(raw) if raw.strip() else {}


def unwrap(got: dict) -> dict:
    """Some routes answer `{ok, job_id}`, some answer `{ok, data:{job_id}}`.

    Both shapes ship today — the art route returns the job body straight out of
    jobs.start(), the music route puts the same body through api.ok() — and a
    caller that knows only one of them dies with a KeyError on the other, which
    is exactly how a panel button came back as a traceback instead of a job id.
    """
    data = got.get("data")
    return data if isinstance(data, dict) else got


def wait_for_job(job_id: int, label: str) -> dict:
    """Poll a background job to completion, printing progress as it moves.

    Prints only when the STAGE CHANGES. A line every three seconds fills the
    pane with the same sentence and pushes the result off the top of it.
    """
    said = ""
    started = time.time()
    while time.time() - started < POLL_MAX_S:
        # UNWRAPPED, because /api/jobs/{id} is one of the api.ok() routes.
        # Read raw, `state` is never found, the loop never sees a terminal
        # job, and a finished image sits at "still processing" until the
        # ten-minute ceiling — which is exactly what a panel showed while
        # the provider had answered in twenty seconds.
        job = unwrap(call("GET", f"/api/jobs/{job_id}"))
        state = str(job.get("state") or "")
        stage = str(job.get("stage") or state)
        if stage and stage != said:
            print(f"  {stage}")
            said = stage
        if state in ("done", "failed", "cancelled"):
            return job
        time.sleep(POLL_EVERY_S)
    print(f"  still running after {int(POLL_MAX_S)}s — "
          f"the {label} job is #{job_id}, poll it from the jobs button")
    return {}


def report(job: dict) -> int:
    """Turn a finished job into the few lines the pane should show."""
    if not job:
        return 0
    result = job.get("result") or {}
    if job.get("state") == "failed" or not result.get("ok", True):
        return refuse("FAILED: " + str(job.get("error") or result.get("error")
                                       or "no reason given"))
    for key in ("path", "installed", "engine_path"):
        if result.get(key):
            print(f"{key}: {result[key]}")
    if result.get("warning"):
        print(f"warning: {result['warning']}")
    art = result.get("artifact") or {}
    if art.get("id"):
        print(f"artifact #{art['id']} rev {art.get('revision', '?')} "
              f"({art.get('status', 'candidate')})")
    if result.get("cost_usd"):
        print(f"cost: ${float(result['cost_usd']):.4f}")
    return 0


# ── what a panel READS ─────────────────────────────────────────────────────
#
# A select can take its options from a button (`options_from` + `options_path`)
# instead of a hardcoded list, and an image-grid takes its pictures the same
# way. That is the difference between picking a task kind and remembering one.
#
# The exact path the renderer walks into a button's output is not documented
# anywhere I can read, so each of these prints the SAME array under several
# plausible keys AND as the bare top-level value. A feed that answers to
# `options`, `values`, `items` and the domain name at once costs a few hundred
# bytes and removes the guess.

def emit(payload: dict, out: str = "") -> int:
    """Hand a panel its data.

    STDOUT IS NOT THE CHANNEL. A routine's log carries the runner's own
    preamble — the step header, the echoed command — so a widget cannot parse
    it, which is why every select first came back "returned no list at
    task_kinds" while the log plainly showed the list. The structured channel
    is `outputs.summary_file`: a JSON file the routine declares and the app
    reads at run end. So this writes the file and ALSO prints it, because the
    log is still where a human looks when a panel misbehaves.
    """
    body = {**payload, "data": dict(payload)}
    text = json.dumps(body, indent=1)
    if out:
        spot = pathlib.Path(out)
        spot.parent.mkdir(parents=True, exist_ok=True)
        spot.write_text(text, encoding="utf-8")
    print(text)
    return 0


def feed(name: str, values: list, out: str = "") -> int:
    """One list, under every key a binding might reach for."""
    return emit({name: values, "options": values, "values": values,
                 "items": values}, out)


def do_art_options(args) -> int:
    got = call("GET", "/api/art/generate/options")
    providers = ["auto"] + [p for p in (got.get("providers") or []) if p != "auto"]
    if args.want == "all":
        # ONE button feeds EVERY select on the panel. Each select names its own
        # options_path into this, so the human presses Load once rather than
        # once per field.
        return whole({"task_kinds": got.get("task_kinds") or [],
                      "sizes": got.get("sizes") or [],
                      "qualities": got.get("qualities") or [],
                      "providers": providers,
                      "refs": ["(none)"] + (got.get("refs") or [])},
                     args.out)
    which = args.want
    values = got.get(which) or []
    if which == "providers":
        # "auto" is a real choice — it means "whichever key is configured" —
        # and it must be FIRST, because it is the right answer nearly always.
        values = ["auto"] + [v for v in values if v != "auto"]
    return feed(which, values, args.out)


def do_music_options(args) -> int:
    data = (call("GET", "/api/music/options") or {}).get("data") or {}
    if args.want == "all":
        models = data.get("models") or []
        default = data.get("default_model")
        if default in models:
            models = [default] + [m for m in models if m != default]
        return whole({"models": models, "takes": takes_list()}, args.out)
    if args.want == "models":
        models = data.get("models") or []
        default = data.get("default_model")
        if default in models:
            models = [default] + [m for m in models if m != default]
        return feed("models", models)
    return feed(args.want, data.get(args.want) or [], args.out)


def takes_list() -> list:
    data = (call("GET", "/api/music/candidates") or {}).get("data") or {}
    rows = (data.get("candidates") or []) + (data.get("kept") or [])
    return [f"#{r.get('artifact_id') or r.get('id')} "
            f"{r.get('logical_name', '')} rev{r.get('revision', '?')}"
            f"{' ' + r['status'] if r.get('status') else ''}"
            for r in rows] or ["(nothing generated yet)"]


def whole(payload: dict, out: str = "") -> int:
    """Several named lists at once — one press fills a whole panel."""
    return emit(payload, out)


def do_music_takes(args) -> int:
    """The candidates, as `#id name rev` strings a select can hold.

    The id is FIRST so the keep routine can take the whole label and split the
    number off it — a select's value is its label, and asking a human to
    retype an artifact id into a number box was the thing that made this panel
    feel like a form.
    """
    data = (call("GET", "/api/music/candidates") or {}).get("data") or {}
    rows = (data.get("candidates") or []) + (data.get("kept") or [])
    takes = [f"#{r.get('artifact_id') or r.get('id')} "
             f"{r.get('logical_name', '')} rev{r.get('revision', '?')}"
             f"{' ' + r['status'] if r.get('status') else ''}"
             for r in rows]
    return feed("takes", takes or ["(nothing generated yet)"], args.out)


def do_art_recent(args) -> int:
    """The art directory as `{label, path}` nodes — the shape the browser reads.

    ESTABLISHED BY EXPERIMENT, not by guessing: a probe panel fed the same
    folder as a bare directory string, a flat list of directories, a nested
    name/path/children tree and this, and only this drew anything. The grid
    beside it then shows whichever node is selected, which is why its `source`
    names the BROWSER and not the button that filled it.
    """
    root = project_root()
    art = (root / ".bgate_out" / "art").resolve()
    if not art.is_dir():
        return emit({"nodes": [], "root": str(art),
                     "note": "no art generated yet"}, args.out)
    # A FOLDER TILE THAT SELECTS TO AN EMPTY GRID IS WORSE THAN NO TILE. The
    # browser drew every top-level directory, and several of them (avatars,
    # characters) keep their art one level down — so picking them showed
    # nothing and the panel read as broken. Two fixes, both about telling you
    # where the art actually is:
    #   * the label carries the image COUNT, so a wall of identical folder
    #     tiles becomes a list you can read;
    #   * a folder holding only subfolders is expanded to those subfolders
    #     rather than offered as an empty dead end.
    # FOLDERS, NOT FILES — the grid says so itself: "pick a folder above".
    # image-grid takes a DIRECTORY and draws what is in it; asset-browser feeds
    # it the chosen one. Pointing the browser at individual image files broke
    # both halves, and walking the whole tree to build that list blew the
    # routine's 30s timeout, so the feed never even got written.
    #
    # What the folder list still owes you, and what the shipped version missed:
    #   * the image COUNT in the label, so tiles are readable rather than 40
    #     identical placeholders (a folder has no thumbnail — that is expected);
    #   * folders whose art sits one level down are expanded to those children,
    #     because selecting an empty parent showed nothing and read as broken;
    #   * a FEWER-IMAGES folder renders BIGGER, since the grid fits tiles to the
    #     pane — which is the only enlarge this widget set has.
    # One level of iterdir per folder: fast enough for the timeout.
    IMG = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

    def images_in(d: pathlib.Path) -> int:
        try:
            return sum(1 for f in d.iterdir()
                       if f.is_file() and f.suffix.lower() in IMG)
        except OSError:
            return 0

    nodes = [{"label": f"· everything · ({images_in(art)})", "path": str(art)}]
    for child in sorted(art.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        n = images_in(child)
        if n:
            nodes.append({"label": f"{child.name} ({n})", "path": str(child)})
            continue
        try:
            deeper = [g for g in sorted(child.iterdir())
                      if g.is_dir() and not g.name.startswith(".") and images_in(g)]
        except OSError:
            deeper = []
        nodes += [{"label": f"{child.name}/{g.name} ({images_in(g)})",
                   "path": str(g)} for g in deeper]
    return emit({"nodes": nodes, "root": str(art)}, args.out)


def do_probe(args) -> int:
    """Emit the SAME art directory under every shape a widget might want.

    The asset-browser and image-grid take a `source`/`tree_path` whose expected
    JSON shape is not documented and is not readable in the packaged app. So
    this ships one payload carrying all of them — a bare directory string, a
    flat list of directories, a nested node tree, label/path objects, and a flat
    list of image files — and the panel points one control at each. Whichever
    control renders is the answer, and it costs one button press to find out.
    """
    root = project_root()
    art = (root / ".bgate_out" / "art").resolve()
    subdirs = sorted(c for c in art.iterdir() if c.is_dir()) if art.is_dir() else []
    pngs = sorted(c for c in art.iterdir()
                  if c.is_file() and c.suffix.lower() == ".png") if art.is_dir() else []
    return emit({
        "root": str(art),
        "folders": [str(art)] + [str(d) for d in subdirs],
        "tree": [{"name": art.name, "path": str(art),
                  "children": [{"name": d.name, "path": str(d), "children": []}
                               for d in subdirs[:20]]}],
        "nodes": [{"label": d.name, "path": str(d)} for d in subdirs[:20]],
        "files": [str(f) for f in pngs[:24]],
        "images": [str(f) for f in pngs[:24]],
    }, args.out)


def do_board_items(args) -> int:
    """Open items as `#id seat title` strings, for a select that steers one of
    them without anybody typing an id — plus the seats, from the same press."""
    state = call("GET", "/api/console/state")
    closed = {"done", "failed", "cancelled", "approved", "rejected"}
    rows = [f"#{i['id']} {i.get('seat', '')} {(i.get('title') or '')[:48]}"
            for i in (state.get("items") or []) if i.get("status") not in closed]
    seats = sorted({str(i.get("seat") or "") for i in (state.get("items") or [])
                    if i.get("seat")})
    return whole({"items": rows or ["(board is empty)"],
                  "seats": seats or ["director", "narrative", "gameplay",
                                     "tech", "art", "audio", "cinematic", "qa"]},
                 args.out)


def project_root() -> "pathlib.Path":
    """Where the dashboard's project actually is, asked of the dashboard.

    An image-grid needs an absolute path, and this CLI runs from whatever
    directory the terminal is in — the project is not inferable from cwd.
    """
    data = (call("GET", "/api/project") or {}).get("data") or {}
    for key in ("root", "cwd"):
        if data.get(key):
            return pathlib.Path(str(data[key]))
    return pathlib.Path(".")


def first_id(label: str) -> str:
    """Pull the id back out of a `#123 seat title` select label.

    An empty box gets its own sentence. A select fed by a Load button holds
    nothing until Load has run and a row is chosen, so "" is the most likely
    thing to arrive here — and `no id in ''` does not tell anybody that.
    """
    raw = str(label or "").strip()
    if not raw:
        raise SystemExit("nothing is selected — press Load, then pick a row "
                         "from the list above this button")
    hit = re.search(r"#(\d+)", raw)
    if not hit:
        raise SystemExit(f"no id in {raw!r} — the list holds labels like "
                         "'#385 office-night-ambient rev2'; press Load to "
                         "refresh it")
    return hit.group(1)


def slug(text: str, fallback: str = "untitled") -> str:
    words = re.findall(r"[a-z0-9]+", str(text or "").lower())[:5]
    return "_".join(words) or fallback


# ── the verbs ──────────────────────────────────────────────────────────────

def do_art(args) -> int:
    prompt = clean(args.prompt)
    # NAMED FROM THE PROMPT when left blank. Inventing a filename is not a
    # decision anybody wants to make before seeing the picture.
    filename = clean(args.filename) or f"{slug(prompt)}.png"
    body = {"prompt": prompt, "filename": filename,
            "task_kind": clean(args.kind), "size": clean(args.size),
            "quality": clean(args.quality),
            "transparent": args.transparent, "tileable": args.tileable}
    if args.provider:
        body["provider"] = args.provider
    picked = clean(args.refs)
    if picked and picked != "(none)":
        body["refs"] = [r.strip() for r in picked.split(",") if r.strip()]
    if body.get("provider") == "auto":
        body.pop("provider")   # blank means "whichever key is configured"
    started = unwrap(call("POST", "/api/art/generate", body))
    if "job_id" not in started:
        return refuse("the dashboard did not start a job: "
                      f"{json.dumps(started)[:400]}")
    print(f"{started.get('provider', '?')} · job #{started['job_id']} · "
          f"{started.get('path', filename)}")
    return report(wait_for_job(int(started["job_id"]), "art"))


def do_music(args) -> int:
    body = {"prompt": clean(args.prompt), "name": clean(args.name),
            "instrumental": args.instrumental}
    model = clean(args.model)
    if model:
        body["model"] = model
    style = clean(args.style)
    if style:
        body["style"] = style
    # DURATION IS V5_5 ONLY. Every other model is charged for its own default
    # length while ignoring the request, so sending it anyway buys a track of
    # the wrong length and reports success. Refused here, before the money.
    seconds = int(args.duration or 0)
    if seconds:
        if model and model != "V5_5":
            return refuse(f"duration is a V5_5 setting and the model is "
                          f"{model} — pick V5_5, or set seconds to 0 to take "
                          f"the model's own length. Nothing was sent.")
        if not model:
            return refuse("duration only applies to V5_5, and no model is "
                          "chosen — press Load and pick V5_5, or set seconds "
                          "to 0. Nothing was sent.")
        body["duration"] = seconds
    started = unwrap(call("POST", "/api/music/generate", body))
    if "job_id" not in started:
        return refuse("the dashboard did not start a job: "
                      f"{json.dumps(started)[:400]}")
    print(f"job #{started['job_id']} — Suno returns several takes; "
          f"audition them with the candidates button")
    done = wait_for_job(int(started["job_id"]), "music")
    code = report(done)
    for track in ((done.get("result") or {}).get("candidates") or []):
        print(f"  #{track.get('artifact_id', '?')} rev {track.get('revision', '?')} "
              f"{track.get('path', '')}")
    return code


def do_candidates(args) -> int:
    got = call("GET", f"/api/music/candidates?logical_name={args.name}"
               if args.name else "/api/music/candidates")
    rows = got.get("candidates") or got.get("data") or []
    if not rows:
        print("no candidates — generate something first")
        return 0
    for row in rows:
        print(f"#{row.get('id')} rev {row.get('revision', '?')} "
              f"{row.get('status', '')} {row.get('path', '')}")
    print("\nkeep one with the keep button (it installs and approves it)")
    return 0


def do_keep(args) -> int:
    got = unwrap(call("POST", "/api/music/keep",
                      {"artifact_id": int(first_id(args.take)), "install": True}))
    print(json.dumps(got, indent=2)[:1200])
    return 0


def do_play(args) -> int:
    """Hand one take to whatever plays audio on this machine.

    THE PANEL CANNOT PLAY IT. `pulsiron-panel/1` has widgets for images and
    sprite sheets and nothing for sound — an `audio` control validates but
    renders as an unsupported-widget placeholder. So auditioning happens in the
    OS player, one click away, rather than not at all.
    """
    data = (call("GET", "/api/music/candidates") or {}).get("data") or {}
    rows = (data.get("candidates") or []) + (data.get("kept") or [])
    if not rows:
        return refuse("there are no takes yet — generate some music first")

    picked = clean(args.take)
    if not picked.strip():
        # NO SELECTION IS NOT AN ERROR HERE. A select fed by a Load button holds
        # nothing until somebody has pressed Load and chosen a row, and refusing
        # to play anything until then makes auditioning a three-step ritual.
        # Playing is harmless and reversible, so an empty box means "the newest
        # one" — and it says which, so nobody wonders what they just heard.
        hit = max(rows, key=lambda r: int(r.get("artifact_id") or r.get("id") or 0))
        print(f"no take picked — playing the newest of {len(rows)}")
    else:
        wanted = first_id(picked)
        hit = next((r for r in rows
                    if str(r.get("artifact_id") or r.get("id")) == wanted), None)
        if not hit:
            return refuse(f"no take #{wanted} — press Load to refresh the list")
    spot = pathlib.Path(hit.get("path") or "")
    if not spot.is_absolute():
        spot = project_root() / spot
    if not spot.is_file():
        return refuse(f"the take is registered but its file is gone: {spot}")
    # `wanted` ONLY EXISTS ON THE PICKED BRANCH. The no-selection path falls back
    # to the newest take and never sets it, so this line raised NameError and the
    # routine died AFTER printing "playing the newest of 8" — the one path the
    # comment above calls harmless was the only one that crashed. Take the id off
    # the row that was actually chosen, which is true on both branches.
    chosen = hit.get("artifact_id") or hit.get("id") or "?"
    print(f"#{chosen} {hit.get('title') or hit.get('logical_name', '')}")
    print(f"  {spot}")
    print(f"  {hit.get('duration', '?')}s · {hit.get('status', 'candidate')}")
    try:
        if sys.platform == "win32":
            os.startfile(str(spot))                      # noqa: S606
        else:
            import subprocess
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, str(spot)])
    except OSError as exc:
        return refuse(f"could not open it: {exc}")
    print("opened in your default player")
    return 0


def do_queue(args) -> int:
    body = {"seat": clean(args.seat), "title": clean(args.title),
            "brief": clean(args.brief),
            "priority": args.priority}
    item = unwrap(call("POST", "/api/queue", body))
    item_id = item.get("id") or item.get("item_id")
    print(f"filed #{item_id} for {args.seat}: {args.title}")
    if args.deploy and item_id:
        sent = call("POST", f"/api/queue/{item_id}/dispatch", {})
        print("dispatched" if sent.get("ok") else
              f"NOT dispatched: {sent.get('error', 'refused')}")
    else:
        print("left queued — auto-deploy picks it up, or press deploy")
    return 0


def do_board(args) -> int:
    state = call("GET", "/api/console/state")
    items = state.get("items") or []
    closed = {"done", "failed", "cancelled", "approved", "rejected"}
    live = [i for i in items if i.get("status") not in closed]
    running = [a for a in (state.get("agents") or [])
               if (a.get("state") or "") == "running"]
    auto = (state.get("autopilot") or {}).get("on")
    print(f"{len(live)} open · {len(running)} agent(s) running · "
          f"auto-deploy {'on' if auto else 'OFF'}")
    for item in live[:args.limit]:
        print(f"  #{item['id']:>4} {item.get('seat', ''):<10} "
              f"{item.get('status', ''):<10} {item.get('title', '')[:60]}")
    for gate in (state.get("gates") or []):
        if gate.get("blocking"):
            print(f"  WAITING ON YOU: #{gate.get('item_id')} {gate.get('title', '')}")
    for question in (state.get("questions") or []):
        print(f"  ASKED YOU: [{question.get('seat', '')}] {question.get('text', '')[:80]}")
    if not auto and live:
        print("\nauto-deploy is off — queued work waits for the deploy button")
    return 0


def do_deploy(args) -> int:
    """Dispatch what is READY, one at a time. Sequential on purpose: a chain
    link whose predecessor has not landed refuses, and firing twenty at once
    turns one concurrency refusal into twenty."""
    state = call("GET", "/api/console/state")
    queued = [i for i in (state.get("items") or [])
              if i.get("status") == "queued" and i.get("ready") is not False]
    if not queued:
        print("nothing ready to deploy")
        return 0
    sent = 0
    for item in queued:
        answer = call("POST", f"/api/queue/{item['id']}/dispatch", {})
        if not answer.get("ok"):
            print(f"stopped at #{item['id']}: {answer.get('error', 'refused')}")
            break
        sent += 1
        print(f"  dispatched #{item['id']} ({item.get('seat')})")
    print(f"{sent} deployed")
    return 0


def do_steer(args) -> int:
    picked = clean(getattr(args, "item", "") or "").strip()
    # AN UNRECOGNISED ITEM IS NOT "EVERYONE". Falling through to steer-all on
    # anything without a '#' meant a stale or mistyped label quietly
    # interrupted every running agent instead of failing.
    if picked and "#" not in picked:
        return refuse(f"{picked!r} is not a board item — press Load and pick "
                      "one, or clear the box to steer every running agent")
    if picked:
        one = first_id(picked)
        got = call("POST", f"/api/queue/{one}/steer", {"text": clean(args.message)})
        print(f"steered #{one}: {json.dumps(got)[:400]}")
        return 0
    got = call("POST", "/api/queue/steer-all", {"text": clean(args.message)})
    print(json.dumps(got.get("data", got))[:800])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="panel_api")
    sub = parser.add_subparsers(dest="verb", required=True)

    art = sub.add_parser("art")
    art.add_argument("--prompt", required=True)
    art.add_argument("--filename", required=True)
    art.add_argument("--kind", default="")
    art.add_argument("--size", default="1024x1024")
    art.add_argument("--quality", default="medium")
    art.add_argument("--provider", default="")
    art.add_argument("--refs", default="")
    # store_true is wrong for a panel: a checkbox sends "true"/"false" as a
    # string either way, so the flag is always present and always truthy.
    art.add_argument("--transparent", type=truthy, default=False)
    art.add_argument("--tileable", type=truthy, default=False)
    art.set_defaults(fn=do_art)

    music = sub.add_parser("music")
    music.add_argument("--prompt", required=True)
    music.add_argument("--name", default="")
    music.add_argument("--style", default="")
    music.add_argument("--model", default="")
    music.add_argument("--duration", type=int, default=0)
    music.add_argument("--instrumental", type=truthy, default=True)
    music.set_defaults(fn=do_music)

    cands = sub.add_parser("candidates")
    cands.add_argument("--name", default="")
    cands.set_defaults(fn=do_candidates)

    keep = sub.add_parser("keep")
    keep.add_argument("--take", required=True,
                      help="a candidates label, e.g. '#385 office-night rev2'")
    keep.set_defaults(fn=do_keep)

    queue = sub.add_parser("queue")
    queue.add_argument("--seat", required=True)
    queue.add_argument("--title", required=True)
    queue.add_argument("--brief", default="")
    queue.add_argument("--priority", type=int, default=5)
    queue.add_argument("--deploy", type=truthy, default=False)
    queue.set_defaults(fn=do_queue)

    board = sub.add_parser("board")
    board.add_argument("--limit", type=int, default=20)
    board.set_defaults(fn=do_board)

    sub.add_parser("deploy").set_defaults(fn=do_deploy)

    art_opts = sub.add_parser("art-options")
    art_opts.add_argument("--want", default="task_kinds")
    art_opts.add_argument("--out", default="",
                       help="write the JSON here for outputs.summary_file")
    art_opts.set_defaults(fn=do_art_options)

    mus_opts = sub.add_parser("music-options")
    mus_opts.add_argument("--want", default="models")
    mus_opts.add_argument("--out", default="",
                       help="write the JSON here for outputs.summary_file")
    mus_opts.set_defaults(fn=do_music_options)

    play = sub.add_parser("play")
    play.add_argument("--take", required=True)
    play.set_defaults(fn=do_play)

    takes = sub.add_parser("music-takes")
    takes.add_argument("--out", default="",
                       help="write the JSON here for outputs.summary_file")
    takes.set_defaults(fn=do_music_takes)

    recent = sub.add_parser("art-recent")
    recent.add_argument("--limit", type=int, default=12)
    recent.add_argument("--out", default="",
                       help="write the JSON here for outputs.summary_file")
    recent.set_defaults(fn=do_art_recent)

    probe = sub.add_parser("probe")
    probe.add_argument("--out", default="")
    probe.set_defaults(fn=do_probe)

    board_items = sub.add_parser("board-items")
    board_items.add_argument("--out", default="",
                       help="write the JSON here for outputs.summary_file")
    board_items.set_defaults(fn=do_board_items)

    steer = sub.add_parser("steer")
    steer.add_argument("--message", required=True)
    steer.add_argument("--item", default="",
                       help="a board label; blank steers every running agent")
    steer.set_defaults(fn=do_steer)

    args = parser.parse_args()
    try:
        return args.fn(args)
    except Down as exc:
        refuse(str(exc))
        return 2


def truthy(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# A binding the panel did not fill in, arriving here as its own source text.
# Three spellings because three are plausible — the panel's dispatch messages
# use {{name}}, the spec validator documents inputs as "$binding", and a
# routine's own command substitution is {name}.
_UNFILLED = re.compile(r"^\s*(?:\$\{?(\w+)\}?|\{\{\s*(\w+)\s*\}\}|\{(\w+)\})\s*$")


def clean(value: str) -> str:
    """Drop a binding that never got substituted, and SAY WHICH FORM LEAKED.

    Without this, an unfilled binding is sent to the API as the literal string
    "$prompt" — which generates a picture of the word, bills for it, and gives
    no hint that the panel wiring is what is wrong. The warning names the exact
    spelling that came through so the spec can be corrected once.
    """
    raw = str(value or "")
    hit = _UNFILLED.match(raw)
    if not hit:
        return raw
    name = next(g for g in hit.groups() if g)
    print(f"warning: the panel did not fill in '{name}' — it arrived as the "
          f"literal {raw.strip()!r}. Treating it as empty.")
    return ""


if __name__ == "__main__":
    sys.exit(main())
