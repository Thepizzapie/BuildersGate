"""bgate — the console entrypoint.

    bgate init NAME [--kind 2d|3d] [--dir DIR] [--pitch TEXT]
                    [--force] [--replace]
                                create a project + a runnable game, and print where
    bgate adopt [DIR] [--name N] [--pitch TEXT] [--kind 2d|3d|2d+3d] [--json]
                                point Builders Gate at a game you ALREADY have.
                                Never scaffolds, never overwrites. (default: .)
    bgate use [DIR|NAME]        make a project the active one for later commands
    bgate projects [--json]     list known projects and which one is active
    bgate serve [--port 7788]   run the dashboard in your browser
    bgate app [--port N]        run the dashboard in a native desktop window
                                (needs: pip install "builders-gate[desktop]")
    bgate publish [--out DIR]   build the arcade: every game, as a static site
    bgate doctor [DIR] [--json] check every external dependency in one pass
    bgate panic [DIR] [--json]  EMERGENCY STOP: kill every agent on a project,
                                reap orphans, and turn auto-deploy off.
                                Works even when the dashboard is gone or wedged.
    bgate hook-install [DIR]    wire lane/lock enforcement into a game project
    bgate hook-status [DIR]     prove the hook is installed AND biting
    bgate hook                  (internal) the PreToolUse hook itself

publish options:
    --out DIR           where to write the site        (default: ./arcade)
    --project P         publish only this project (repeatable; name or path)
    --rebuild MODE      stale | always | never         (default: stale)
    --host NAME         cloudflare | netlify | github | itch | none
                        whose per-file upload limit to respect, and whether to
                        pre-compress the files that break it (default: cloudflare)
    --config FILE       site settings                  (default: ./arcade.json)
    --dry-run           list what would ship, write nothing
    --force             publish into a non-empty directory we did not create
    --serve [PORT]      preview the built site locally (default port 8000)
    --json              machine-readable report
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Bash is in the matcher because dispatch grants the agent Bash: guarding only
# the file-edit tools left `echo x > game/foo.gd` as an open door through every
# lane and lock the README advertises.
HOOK_MATCHER = "Bash|Write|Edit|MultiEdit|NotebookEdit"

# `python -m`, never sys.executable. This file is COMMITTED into the game repo,
# so an absolute interpreter path bakes one machine's venv into everyone else's
# checkout — where it silently fails to run and enforcement quietly stops.
HOOK_COMMAND = "python -m bgate_cli.hook"

HOOK_CONFIG = {
    "matcher": HOOK_MATCHER,
    "hooks": [{"type": "command", "command": HOOK_COMMAND}],
}

# SessionStart carries what `instructions` structurally cannot: the MCP field is
# fixed when the stdio server boots, so it can state the role and never the
# situation — which board items are queued, whether the dashboard is even up to
# run them, which files another live session is already holding. A director that
# must ask three questions before it can act will skip them.
#
# `clear` and `compact` are on the matcher with `startup` and `resume` for the
# same reason: those are precisely the moments the context is discarded, and
# re-arriving with no idea what is on the board is the lobotomy this closes.
SESSION_MATCHER = "startup|resume|clear|compact"
SESSION_COMMAND = "python -m bgate_cli.session"
SESSION_CONFIG = {
    "matcher": SESSION_MATCHER,
    "hooks": [{"type": "command", "command": SESSION_COMMAND}],
}

# event -> (config, module fragment that identifies OUR entry for that event)
HOOK_EVENTS = {
    "PreToolUse": (HOOK_CONFIG, "bgate_cli.hook"),
    "SessionStart": (SESSION_CONFIG, "bgate_cli.session"),
}


def _is_bgate_hook(entry: dict, needle: str = "bgate_cli.hook") -> bool:
    return any(needle in h.get("command", "")
               for h in entry.get("hooks", []))


def _pin(config: dict) -> dict:
    """The same entry with the interpreter pinned, for user scope. See below."""
    return {**config, "hooks": [
        {**h, "command": h["command"].replace(
            "python -m ", f'"{sys.executable}" -m ', 1)}
        for h in config["hooks"]]}


def _user_hook_config() -> dict:
    """The same gate, addressed to ONE machine instead of one repo.

    The `python -m` rule above exists because the project copy is COMMITTED and
    an absolute interpreter path would bake this machine's venv into everyone
    else's checkout. ~/.claude/settings.json is committed nowhere and shared with
    nobody, so that argument does not apply — and the opposite hazard does. A
    bare `python` resolves against whatever is first on PATH when the hook fires,
    which is routinely not the environment bgate was installed into; the hook
    then dies on ModuleNotFoundError, fails open, and enforcement stops with no
    symptom but a line in hook.log. This is the same lesson `claude mcp add`
    already carries in CLAUDE.md: use the absolute interpreter.
    """
    return {
        "matcher": HOOK_MATCHER,
        "hooks": [{"type": "command",
                   "command": f'"{sys.executable}" -m bgate_cli.hook'}],
    }


def install_hook(project_dir: str, scope: str = "project") -> dict:
    """Merge the enforcement hook into a settings.json.

    scope="project" writes <project>/.claude/settings.json — the committed,
    per-repo gate. scope="user" writes ~/.claude/settings.json ONCE and covers
    every Builders Gate project on the machine, including ones that do not exist
    yet.

    USER SCOPE WORKS BECAUSE THE HANDLER WAS ALWAYS PROJECT-AGNOSTIC. It never
    read an installed-at path: it resolves the project by walking up from the
    file being written (hook.py `db.resolve_root(target_path.parent)`) and
    returns ALLOW when that finds nothing, so a write outside any game project
    is untouched. The per-project install was therefore never enforcing
    anything the user-scope one cannot — it was only ever a per-repo switch, and
    a switch you must remember to flip in each new project is a switch that is
    off exactly when a fresh project needs it most.

    Merges rather than overwrites — a game project may already carry its own
    hooks, and clobbering them is exactly the kind of stomp this tool polices.
    An entry we wrote on an earlier version IS rewritten, because a stale
    matcher (or an absolute interpreter path from another machine) is a gate
    that no longer gates.
    """
    if scope not in ("project", "user"):
        return {"ok": False, "error": f"unknown scope {scope!r}; use project|user"}
    if scope == "user":
        settings_path = Path.home() / ".claude" / "settings.json"
    else:
        settings_path = Path(project_dir) / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"ok": False,
                    "error": f"{settings_path} exists but is not valid JSON — "
                             "fix it by hand; refusing to overwrite"}

    hooks = settings.setdefault("hooks", {})
    installed: list[str] = []
    updated: list[str] = []
    commands: dict[str, str] = {}
    for event, (base, needle) in HOOK_EVENTS.items():
        config = _pin(base) if scope == "user" else base
        commands[event] = config["hooks"][0]["command"]
        bucket = hooks.setdefault(event, [])
        ours = [entry for entry in bucket if _is_bgate_hook(entry, needle)]
        if not ours:
            bucket.append(config)
            installed.append(event)
            continue
        for entry in ours:
            if entry != config:
                entry.clear()
                entry.update(config)
                if event not in updated:
                    updated.append(event)
        # A duplicate entry would run the hook twice per event; keep the first.
        for extra in ours[1:]:
            bucket.remove(extra)
            if event not in updated:
                updated.append(event)
    if installed or updated:
        settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "scope": scope,
        "settings": str(settings_path),
        # Kept as booleans as well as lists: callers (and tests) were reading
        # these two keys before SessionStart existed, and quietly changing what
        # `installed` means is how a passing test stops meaning anything.
        "installed": bool(installed),
        "updated": bool(updated),
        "events_installed": installed,
        "events_updated": updated,
        "matcher": HOOK_MATCHER,
        "command": commands["PreToolUse"],
        "commands": commands,
        "covers": ("every Builders Gate project on this machine, including ones "
                   "not created yet" if scope == "user"
                   else str(Path(project_dir).resolve())),
        "note": "PreToolUse enforces lanes/locks (BGATE_DIRECTOR_MODE controls "
                "how hard for a seatless session); SessionStart preloads the "
                "board. `bgate hook-status` proves the first one is biting, "
                "`bgate session-start --print` shows what the second injects.",
    }


def hook_status(project_dir: str = "", as_json: bool = False) -> int:
    """Run the hook's own probes and print the verdict. Exit 1 if not enforcing.

    The hook fails open on purpose, so its silence proves nothing. This is the
    one command that answers 'is anything actually being enforced right now'.
    """
    from bgate_cli import hook

    report = hook.selftest(project_dir or None)
    if as_json:
        print(json.dumps(report, indent=2))
        return 0 if report["enforcing"] else 1

    print(f"project   {report['project_root'] or '(none)'}")
    if report.get("seated"):
        print(f"seat      {report['seat']}")
    else:
        print(f"seat      (none adopted) -> director, mode={report.get('mode')}")
    print(f"installed {'yes' if report['installed'] else 'NO'}"
          + (f"  matcher={report.get('matchers') or []}" if report["installed"] else ""))
    for probe in report["probes"]:
        mark = "ok  " if probe.get("ok") else "FAIL"
        print(f"{mark}  {probe['probe']}: {probe.get('error') or probe.get('got')}")
    if report["recent_failures"]:
        print(f"\n{len(report['recent_failures'])} recent FAIL-OPEN event(s) — "
              "writes went through unchecked:")
        for row in report["recent_failures"]:
            print(f"  {row.get('ts', '?')}  {row.get('detail', '')[:120]}")
    print()
    print(report.get("reason", ""))
    return 0 if report["enforcing"] else 1


def init_project(name: str, kind: str = "2d", dest: str = "", pitch: str = "",
                 force: bool = False, replace: bool = False) -> int:
    """Create the project store AND a runnable game, then say where it landed.

    The first-run gap the audit named: the only way to make a project was an MCP
    session calling project_init, which never printed the directory it wrote to.
    One command, one absolute path on stdout — that path is the whole point, so
    it is printed even when the scaffold had nothing new to write.
    """
    from bgate_core import project, scaffold
    from bgate_core.util import slugify

    if kind not in scaffold.KINDS:
        print(f"error: --kind must be one of {'|'.join(scaffold.KINDS)}, got {kind!r}")
        return 2
    if not name.strip():
        print("error: a project needs a name — bgate init <name>")
        return 2

    # Default to a NEW directory under the cwd rather than the cwd itself: a
    # scaffolder that unpacks a game into whatever directory you happened to be
    # standing in is a data-loss bug wearing a feature's hat.
    root = Path(dest).expanduser().resolve() if dest else (
        Path.cwd() / slugify(name)).resolve()

    try:
        made = scaffold.new_project(root, name, kind=kind, force=force,
                                    replace=replace)
    except FileExistsError as exc:
        print(f"error: {exc}")
        return 1
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}")
        return 2

    project.init(root, name, pitch=pitch, engine="godot", dimension=kind)

    print(f"created {name} ({kind}) — {len(made['files'])} files")
    # SAY WHAT WAS PROTECTED, or a careful run reads as a broken one. `force`
    # now fills in what is missing and leaves anything the user has edited
    # alone, so a top-up over a live project legitimately writes nothing —
    # and "created 0 files" with no further word looks like the command
    # failed rather than like it declined to overwrite someone's work.
    if made.get("note"):
        print(made["note"])
    for entry in made.get("skipped") or []:
        print(f"  kept your {entry['file']} — {entry['reason']}")
    for entry in made.get("replaced") or []:
        print(f"  replaced {entry['file']} (backup: {entry['backup']})")
    print(str(root))
    print()
    print("next:")
    print(f"  cd {root}")
    print("  bgate serve            open the dashboard on this project")
    print("  bgate doctor           check the toolchain (godot, blender, ...)")
    return 0


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f}MB"


def adopt_project(directory: str = "", name: str = "", pitch: str = "",
                  kind: str = "", as_json: bool = False) -> int:
    """Adopt an EXISTING game and print what we understood about it.

    The printout is not decoration. The person running this has months of work
    in the directory and is being asked to trust a tool that just wrote to it;
    showing that we found their project.godot, counted their scenes and got the
    dimension right is the only evidence available at this point that we read
    the project rather than replaced it.
    """
    from bgate_core import adopt as _adopt
    from bgate_core import project

    target = Path(directory).expanduser().resolve() if directory else Path.cwd()

    if kind and kind not in project.DIMENSIONS:
        print(f"error: --kind must be one of {'|'.join(project.DIMENSIONS)}, "
              f"got {kind!r}")
        return 2

    try:
        report = _adopt.adopt(target, name=name, pitch=pitch,
                              dimension=kind or None)
    except FileExistsError as exc:
        print(f"error: {exc}")
        return 1
    except (NotADirectoryError, ValueError) as exc:
        print(f"error: {exc}")
        return 2

    if as_json:
        print(json.dumps(report, indent=2))
        return 0

    found = report["detected"]
    proj = report["project"]
    verb = "re-adopted" if report["already_adopted"] else "adopted"
    print(f"{verb} {proj['name']} — {report['path']}")
    print()
    if found["godot"]:
        version = f" {found['godot_version']}" if found["godot_version"] else ""
        print(f"  godot{version}      {found['godot_dir']}")
        if found["main_scene"]:
            print(f"  main scene    {found['main_scene']}")
    else:
        print("  godot         NOT FOUND — no project.godot at or under this "
              "directory,")
        print("                so engine was recorded as 'none' and the godot_* "
              "tools")
        print("                will stay unavailable until one exists.")
    evidence = found["dimension_evidence"]
    print(f"  dimension     {proj['dimension']}  "
          f"({evidence['3d_nodes']} 3D nodes / {evidence['2d_nodes']} 2D nodes "
          "seen in scenes)")
    print(f"  scenes        {found['scenes']}"
          + (f"   biggest: {', '.join(found['biggest_scenes'][:3])}"
             if found["biggest_scenes"] else ""))
    print(f"  scripts       {found['scripts']}")
    print(f"  assets        {found['images']} images, {found['audio']} audio, "
          f"{found['models']} models")
    print(f"  size          {found['files']} files, {_mb(found['bytes'])}")
    if found["top_dirs"]:
        print(f"  layout        {', '.join(found['top_dirs'][:10])}")
    print()
    for label, row in report["written"].items():
        if row.get("error"):
            print(f"  !     {label}: {row['error']}")
        else:
            print(f"  {row['action'].ljust(9)} {row['path']}")
    print()
    if not proj.get("pitch"):
        print("no pitch recorded — the bible starts empty without one. Set it:")
        print(f'  bgate adopt "{report["path"]}" --pitch "what this game is"')
        print()
    print("next:")
    print(f"  cd {report['path']}")
    print("  bgate doctor           check the toolchain (godot, blender, ...)")
    print("  bgate serve            open the dashboard on this project")
    print("  read CLAUDE.md         it tells your Claude session how to work here")
    return 0


def use_project(token: str = "", as_json: bool = False) -> int:
    """Make a project the active one, persistently.

    Persistently, and OUTSIDE the repo (~/.bgate/active.json): the alternative
    people were left with was exporting BGATE_ROOT in every shell, which is
    invisible, per-terminal, and the first thing anyone forgets.
    """
    from bgate_core import project

    target = token or "."
    try:
        resolved = _resolve_project(target)
    except LookupError as exc:
        print(f"error: {exc}")
        return 2
    try:
        root = project.set_active(resolved)
        record = project.get(root)
    except LookupError as exc:
        print(f"error: {exc}")
        return 1

    if as_json:
        print(json.dumps({"active": str(root), "project": record}, indent=2))
        return 0
    print(f"active project: {record['name']} ({record['slug']})")
    print(str(root))
    print()
    print("this is what `bgate serve`, `bgate doctor` and the MCP tools will")
    print("use when nothing more specific says otherwise. An explicit")
    print("project_dir=... on a tool call, BGATE_ROOT, or standing inside a")
    print("different project all still win over it, in that order.")
    return 0


def list_projects(as_json: bool = False) -> int:
    """Every known project, with the active one marked."""
    from bgate_core import db, project

    known = project.known_projects()
    active = project.active_root()
    here = db.resolve_root()

    rows = []
    for name, path in sorted(known.items()):
        try:
            record = project.get(path)
            title, dimension = record["name"], record["dimension"]
        except Exception:  # a project whose DB is unreadable still gets listed
            title, dimension = name, "?"
        rows.append({
            "slug": name, "name": title, "path": path, "dimension": dimension,
            "active": active is not None and Path(path) == active,
            "cwd": here is not None and Path(path) == here,
        })

    if as_json:
        print(json.dumps({"projects": rows,
                          "active": str(active) if active else None}, indent=2))
        return 0
    if not rows:
        print("no known projects.")
        print()
        print("  bgate init NAME     start a new game from a template")
        print("  bgate adopt DIR     point Builders Gate at a game you already have")
        return 0

    width = max(len(row["name"]) for row in rows)
    for row in rows:
        mark = "*" if row["active"] else ("." if row["cwd"] else " ")
        print(f"{mark} {row['name'].ljust(width)}  {row['dimension'].ljust(6)} "
              f"{row['path']}")
    print()
    print("* active (bgate use)   . the project your cwd is inside")
    return 0


def _resolve_project(token: str) -> str:
    """A --project value, as a path. Accepts a registry name or a directory."""
    from bgate_core import project

    known = project.known_projects()
    if token in known:
        return known[token]
    path = Path(token).expanduser()
    if path.is_dir():
        return str(path.resolve())
    raise LookupError(
        f"no project named {token!r} and no directory at that path. "
        f"Known: {', '.join(sorted(known)) or '(none)'}")


def _parse_headers_file(path: Path) -> list[tuple[str, dict]]:
    """The site's _headers as [(url pattern, {header: value})].

    Only the subset this project writes: a rule line starting with '/', then
    indented "Name: value" lines. Comments and blanks ignored.
    """
    rules: list[tuple[str, dict]] = []
    if not path.is_file():
        return rules
    current: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            current = {}
            rules.append((line.strip(), current))
        elif ":" in line and rules:
            name, _, value = line.strip().partition(":")
            current[name.strip()] = value.strip()
    return rules


def preview(out: str, port: int = 8000) -> int:
    """Serve the built site with the SAME headers the host will send.

    A plain `python -m http.server` is NOT a preview of production here, and the
    gap is not cosmetic: files that were pre-compressed to fit the host's upload
    limit keep their original names, so a server that does not read _headers
    hands the browser gzip bytes labelled application/wasm and the game dies at
    the loader. This reads the generated _headers and applies it.
    """
    import fnmatch
    import functools
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    root = Path(out).resolve()
    if not (root / "index.html").is_file():
        print(f"error: nothing to serve — {root} has no index.html "
              "(run bgate publish first)")
        return 1

    rules = _parse_headers_file(root / "_headers")

    class Handler(SimpleHTTPRequestHandler):
        def _rules(self) -> dict:
            requested = self.path.split("?")[0]
            merged: dict = {}
            for pattern, headers in rules:
                if fnmatch.fnmatch(requested, pattern):
                    merged.update(headers)
            return merged

        def guess_type(self, path):
            # Content-Type has to come from the rule rather than end_headers,
            # or the response carries two of them — the base handler already
            # emitted its guess by the time end_headers runs.
            return self._rules().get("Content-Type") or super().guess_type(path)

        def end_headers(self):
            for name, value in self._rules().items():
                if name.lower() != "content-type":
                    self.send_header(name, value)
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def log_message(self, fmt, *args):  # one line per request, no noise
            sys.stderr.write(f"  {args[0] if args else ''}\n")

    handler = functools.partial(Handler, directory=str(root))
    with ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"arcade preview · http://127.0.0.1:{port}")
        print(f"  serving {root}")
        print("  ctrl-c to stop")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print()
    return 0


def publish(out: str = "", projects: list[str] | None = None,
            rebuild: str = "stale", config: str = "", host: str = "cloudflare",
            force: bool = False, dry_run: bool = False, as_json: bool = False,
            serve_port: int = 0) -> int:
    """Build the arcade and say what shipped, what did not, and why.

    The "what did not, and why" half is the point. A publish that silently drops
    a game — no Godot project, hidden, export failed — looks identical to a
    publish that worked, and you find out from a player.
    """
    import bgate_site

    target = str(Path(out).expanduser().resolve()) if out else \
        str((Path.cwd() / "arcade").resolve())

    roots = None
    if projects:
        try:
            roots = [_resolve_project(token) for token in projects]
        except LookupError as exc:
            print(f"error: {exc}")
            return 2

    report = bgate_site.build(target, roots=roots, rebuild=rebuild,
                              config=config or None, host=host, force=force,
                              dry_run=dry_run)

    if as_json:
        print(json.dumps(report, indent=2))
    elif not report.get("ok"):
        print(f"error: {report.get('error', 'publish failed')}")
    else:
        verb = "would publish" if dry_run else "published"
        for game in report["games"]:
            size = game["bytes"] / (1024 * 1024)
            print(f"  ok    {game['slug'].ljust(22)} {size:6.1f}MB  "
                  f"{game['url']}")
        for row in report["skipped"]:
            print(f"  skip  {str(row['slug']).ljust(22)} {row['reason']}")
        for row in report["errors"]:
            print(f"  FAIL  {str(row['slug']).ljust(22)} "
                  f"{row['stage']}: {row['error']}")
        for name in report["pruned"]:
            print(f"  gone  {name.ljust(22)} removed (no longer publishable)")
        for row in report.get("compressed", []):
            print(f"  gzip  {row['url'].ljust(22)} "
                  f"{row['was'] / (1024 * 1024):.1f}MB -> "
                  f"{row['now'] / (1024 * 1024):.1f}MB "
                  f"(over the host's per-file limit raw)")
        total = report["bytes"] / (1024 * 1024)
        print()
        print(f"{verb} {len(report['games'])} game(s), {total:.1f}MB "
              f"in {report['seconds']}s")
        if not dry_run and report["games"]:
            print(f"  {report['out']}")
            print()
            print("next:")
            print("  bgate publish --serve           preview it locally")
            if report.get("deploy"):
                print(f"  {report['deploy']}")

    if not report.get("ok"):
        return 1
    if report["errors"]:
        return 1
    if serve_port and not dry_run:
        print()
        return preview(target, serve_port)
    return 0


def panic(project_dir: str = "", as_json: bool = False) -> int:
    """THE KILL SWITCH, from a terminal. Stop every agent on a project.

    This exists as a CLI command and not only as a button because the moment
    you need it is exactly the moment the dashboard may be the thing that is
    wedged — or not running at all, while the agents it spawned very much are.
    The pid ledger lives in the project (``.bgate/agents/``), so this works
    against a dashboard that is already gone.

    Turns auto-deploy off first (otherwise the loop dispatches a replacement
    into the gap), kills each agent's whole process tree, reaps anything the
    ledger knows about, and settles the items so the board stops claiming work
    is running. Exit 0 even when nothing was running — this is the command you
    hammer, and "nothing to kill" is a success.
    """
    from bgate_ui import dispatch as _dispatch

    root = project_dir or os.environ.get("BGATE_ROOT") or ""
    if not root:
        try:
            from bgate_core import project
            root = str(project.require_root())
        except Exception:
            print("no project here — run this inside a game project, "
                  "or pass the directory: bgate panic <DIR>")
            return 2

    result = _dispatch.kill_all(str(root), reason="bgate panic", actor="cli")
    if as_json:
        print(json.dumps(result, indent=2))
        return 0
    stopped, orphans = result.get("stopped") or [], result.get("orphans") or []
    print(f"stopped {len(stopped)} running agent(s)"
          + (f": {', '.join('#' + str(i) for i in stopped)}" if stopped else ""))
    print(f"reaped  {len(orphans)} orphaned process(es)")
    if result.get("autopilot"):
        print("auto-deploy is now OFF — turn it back on from the console")
    settled = result.get("settled") or []
    if settled:
        print(f"settled {len(settled)} item(s) that were stuck 'dispatched'")
    for problem in result.get("errors") or []:
        print(f"  ! {problem}")
    if not stopped and not orphans:
        print("nothing was running.")
    return 0


def doctor(project_dir: str = "", as_json: bool = False) -> int:
    """Print the dependency report. Exit 1 if anything is unavailable.

    The exit code is the point: this is meant to be the one line a setup script
    or a CI step runs to decide whether the toolchain is usable, without
    grepping five status commands' output for the word "not found".
    """
    from bgate_core import doctor as _doctor

    root = project_dir or os.environ.get("BGATE_ROOT") or ""
    if not root:
        try:  # a cwd inside a project is the common case; not being in one is fine
            from bgate_core import project
            root = str(project.require_root())
        except Exception:
            root = ""

    report = _doctor.check(root or None, refresh=True)
    if as_json:
        # Nested under its own key, not merged: the top level of this document is
        # one row per dependency and a consumer that iterates it would read
        # "settings" as a missing binary.
        print(json.dumps({**report,
                          "settings": _doctor.settings_report(root or None)},
                         indent=2))
    else:
        width = max(len(name) for name in report)
        for name in _doctor.CHECKS:
            row = report[name]
            mark = "ok  " if row["available"] else "MISS"
            detail = row["version"] or row["reason"]
            if row["available"] and row["path"]:
                detail = f"{detail}  [{row['path']}]" if detail else row["path"]
            print(f"{mark}  {name.ljust(width)}  {detail}")
        print()
        print(_doctor.summary(report))
        # The other half of "why is this board not doing what I told it": an env
        # var in a shell profile silently winning over what the panel shows.
        # AFTER the summary and deliberately outside the exit code — a setting
        # that is merely non-default is not a missing dependency.
        print()
        _doctor.print_settings(root or None)
    return 0 if all(row["available"] for row in report.values()) else 1


def _writable_console() -> None:
    """Stop the Windows console mangling our own prose.

    Reason strings carry em dashes like every other string in this codebase, and
    a stock Windows console is cp1252 — so `bgate doctor` printed its advice as
    mojibake, which is a poor first impression for the command people run when
    something is already wrong. Best-effort: a stream that cannot be
    reconfigured is left alone rather than crashing the CLI over punctuation.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def main() -> int:
    _writable_console()
    args = sys.argv[1:]
    cmd = args[0] if args else "help"

    if cmd == "init":
        rest = args[1:]

        def opt(flag: str, default: str = "") -> str:
            if flag in rest:
                index = rest.index(flag) + 1
                if index < len(rest):
                    return rest[index]
            return default

        flagged = {"--kind", "--dir", "--pitch"}
        skip: set[int] = set()
        for i, token in enumerate(rest):
            if token in flagged:
                skip.update({i, i + 1})
        positional = [a for i, a in enumerate(rest)
                      if i not in skip and not a.startswith("-")]
        if not positional:
            print(__doc__)
            return 2
        return init_project(positional[0], kind=opt("--kind", "2d"),
                            dest=opt("--dir"), pitch=opt("--pitch"),
                            # --replace is the only way to overwrite from the
                            # command line. --force stopped meaning that when it
                            # was found destroying customised files in place —
                            # export_presets.cfg is gitignored by the template
                            # this ships, so for anyone with custom export
                            # targets it was unrecoverable. Deliberate
                            # replacement is still legitimate; it just has to be
                            # asked for, and it takes a .bak first.
                            force="--force" in rest or "--replace" in rest,
                            replace="--replace" in rest)

    if cmd == "adopt":
        rest = args[1:]

        def opt(flag: str, default: str = "") -> str:
            if flag in rest:
                index = rest.index(flag) + 1
                if index < len(rest):
                    return rest[index]
            return default

        flagged = {"--name", "--pitch", "--kind"}
        skip: set[int] = set()
        for i, token in enumerate(rest):
            if token in flagged:
                skip.update({i, i + 1})
        positional = [a for i, a in enumerate(rest)
                      if i not in skip and not a.startswith("-")]
        return adopt_project(positional[0] if positional else "",
                             name=opt("--name"), pitch=opt("--pitch"),
                             kind=opt("--kind"), as_json="--json" in rest)

    if cmd in ("use", "switch", "select"):
        positional = [a for a in args[1:] if not a.startswith("-")]
        return use_project(positional[0] if positional else "",
                           as_json="--json" in args)

    if cmd == "projects":
        return list_projects(as_json="--json" in args)

    if cmd == "publish":
        rest = args[1:]

        def value(flag: str, default: str = "") -> str:
            if flag in rest:
                index = rest.index(flag) + 1
                if index < len(rest) and not rest[index].startswith("-"):
                    return rest[index]
            return default

        repeated = [rest[i + 1] for i, token in enumerate(rest)
                    if token == "--project" and i + 1 < len(rest)
                    and not rest[i + 1].startswith("-")]

        # --serve takes an optional port, so it cannot use value()'s "missing
        # means empty" rule: `--serve` alone is a request, not an omission.
        port = 0
        if "--serve" in rest:
            given = value("--serve")
            try:
                port = int(given) if given else 8000
            except ValueError:
                print(f"error: --serve wants a port number, got {given!r}")
                return 2

        mode = value("--rebuild", "stale")
        from bgate_site import HOSTS, REBUILD_MODES
        if mode not in REBUILD_MODES:
            print(f"error: --rebuild must be one of {'|'.join(REBUILD_MODES)}, "
                  f"got {mode!r}")
            return 2
        where = value("--host", "cloudflare")
        if where not in HOSTS:
            print(f"error: --host must be one of {'|'.join(HOSTS)}, got {where!r}")
            return 2

        return publish(out=value("--out"), projects=repeated, rebuild=mode,
                       config=value("--config"), host=where,
                       force="--force" in rest, dry_run="--dry-run" in rest,
                       as_json="--json" in rest, serve_port=port)

    if cmd == "doctor":
        positional = [a for a in args[1:] if not a.startswith("-")]
        return doctor(positional[0] if positional else "", as_json="--json" in args)

    if cmd in ("panic", "stop-all", "killswitch"):
        positional = [a for a in args[1:] if not a.startswith("-")]
        return panic(positional[0] if positional else "",
                     as_json="--json" in args)

    if cmd == "hook":
        from bgate_cli.hook import main as hook_main
        return hook_main()

    if cmd in ("session-start", "session"):
        from bgate_cli.session import main as session_main
        return session_main(args[1:] or ["--print"])

    if cmd == "hook-install":
        rest = args[1:]
        scope = "project"
        if "--scope" in rest:
            i = rest.index("--scope")
            scope = rest[i + 1] if i + 1 < len(rest) else ""
            # Drop the flag AND its value, or `--scope project ./game` reads
            # "project" as the directory and installs into ./project.
            rest = rest[:i] + rest[i + 2:]
        positional = [a for a in rest if not a.startswith("-")]
        # `--scope user` takes no directory: it is not about a directory.
        target = "." if scope == "user" else (positional[0] if positional else ".")
        result = install_hook(target, scope=scope)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1

    if cmd == "hook-status":
        positional = [a for a in args[1:] if not a.startswith("-")]
        return hook_status(positional[0] if positional else "",
                           as_json="--json" in args)

    if cmd == "serve":
        port = 7788
        if "--port" in args:
            port = int(args[args.index("--port") + 1])
        from bgate_ui.app import serve
        serve(port=port)
        return 0

    if cmd == "app":
        port = None
        if "--port" in args:
            port = int(args[args.index("--port") + 1])
        from bgate_ui.desktop import run as run_desktop
        return run_desktop(port=port, debug="--debug" in args)

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
