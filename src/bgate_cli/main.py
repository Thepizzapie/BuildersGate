"""bgate, the console entrypoint.

    bgate init NAME [--kind 2d|3d] [--engine godot|web] [--dir DIR] [--pitch TEXT] [--without floor,music,...]
                    [--brief FILE] [--ref IMAGE ...] [--force] [--replace]
                                create a project + a runnable game, and print where
    bgate adopt [DIR] [--name N] [--pitch TEXT] [--kind 2d|3d|2d+3d] [--json] [--without floor,music,...]
                [--brief FILE] [--ref IMAGE ...]
                                point Builders Gate at a game you ALREADY have.
                                Never scaffolds, never overwrites. (default: .)
    bgate use [DIR|NAME]        make a project the active one for later commands
    bgate projects [--json]     list known projects and which one is active
    bgate serve [--port 7788]   run the dashboard in your browser
    bgate app [--port N]        run the dashboard in a native desktop window
                                (needs: pip install "builders-gate[desktop]")
    bgate app --remote          the window, plus phone access over Tailscale
    bgate publish [--out DIR]   build the arcade: every game, as a static site
    bgate connect [CLIENT...] [--all] [--check] [--show] [--remove] [--json]
                                wire your coding agent to the Builders Gate MCP
                                server, pinned to THIS interpreter. With no
                                argument it writes nothing and reports every
                                client it knows: claude, codex, gemini, vscode,
                                cursor, windsurf, opencode.
                                --show prints the config block for clients that
                                have no `mcp add` of their own, and for any MCP
                                client not on that list.
    bgate doctor [DIR] [--json] check every external dependency in one pass
    bgate key [--json]          show every provider key and which layer supplies it
    bgate key set PROVIDER [--global]
                                store a key. Prompted, never taken as an argument.
                                --global writes ~/.bgate/.env, which every project
                                on this machine inherits and which works with no
                                project at all; without it, this project only.
    bgate key clear PROVIDER [--global]
                                forget a key from that store
    bgate panic [DIR] [--json]  EMERGENCY STOP: kill every agent on a project,
                                reap orphans, and turn auto-deploy off.
                                Works even when the dashboard is gone or wedged.
    bgate kit [list | install NAME [--force] [--no-bind] | remove NAME [--force]] [--project DIR]
                                reusable systems (controllers, inventory, health)
                                copied into the active game, never overwriting
    bgate library [list | search TEXT | publish PATH... [--tags a,b] [--collection C]
                   | import ID... [--dest DIR] | forget ID] [--project DIR]
                                the machine-wide asset library at ~/.bgate/library:
                                what one game made, another can take
    bgate hook-install [DIR]    wire lane/lock enforcement into a game project
    bgate hook-uninstall [DIR]  remove it again, leaving your other hooks alone
    bgate un-adopt [DIR] --yes  delete this project's .bgate store; game untouched
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
#
# Read/Glob/Grep/NotebookRead are here for the CONTAINMENT gate and nothing
# else - lanes and locks have no opinion on a read. They earn the slot because
# "this agent cannot touch anything outside its project" is not a claim about
# writing: reading is how another game's design docs and unreleased plot end up
# in a transcript. A hook that never sees the call cannot judge it, so the
# matcher is the only place that coverage can come from. Widening it also
# upgrades existing installs on the next `bgate hook-install`, which rewrites
# our own entry whenever it differs.
HOOK_MATCHER = ("Bash|Write|Edit|MultiEdit|NotebookEdit"
                "|Read|NotebookRead|Glob|Grep")

# `python -m`, never sys.executable. This file is COMMITTED into the game repo,
# so an absolute interpreter path bakes one machine's venv into everyone else's
# checkout, where it silently fails to run and enforcement quietly stops.
HOOK_COMMAND = "python -m bgate_cli.hook"

HOOK_CONFIG = {
    "matcher": HOOK_MATCHER,
    "hooks": [{"type": "command", "command": HOOK_COMMAND}],
}

# SessionStart carries what `instructions` structurally cannot: the MCP field is
# fixed when the stdio server boots, so it can state the role and never the
# situation, which board items are queued, whether the dashboard is even up to
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
    """The same entry with the interpreter pinned, user scope only.

    The configs above say `python -m` because the project copy is COMMITTED, and
    an absolute interpreter path would bake this machine's venv into everyone
    else's checkout. ~/.claude/settings.json is committed nowhere and shared with
    nobody, so that argument does not apply, and the opposite hazard does. A
    bare `python` resolves against whatever is first on PATH when the hook fires,
    which is routinely not the environment bgate was installed into; the hook
    then dies on ModuleNotFoundError, fails open, and enforcement stops with no
    symptom but a line in hook.log. This is the same lesson `claude mcp add`
    already carries in CLAUDE.md: use the absolute interpreter.
    """
    return {**config, "hooks": [
        {**h, "command": h["command"].replace(
            "python -m ", f'"{sys.executable}" -m ', 1)}
        for h in config["hooks"]]}


def uninstall_hook(project_dir: str, scope: str = "project") -> dict:
    """Remove OUR PreToolUse entry, and nothing else.

    There was no way back out. Installing wrote into a settings.json the user
    may share with other tooling, and backing it out meant hand-editing JSON -
    which is a poor answer to "can I cleanly remove this", and the first
    question anyone careful asks before installing anything.

    Surgical by the same rule install_hook merges by: only entries whose
    command mentions ``bgate_cli.hook`` are dropped, an entry that ends up with
    no hooks left is dropped with it, and a settings file we did not write into
    is reported untouched rather than rewritten. `hooks` and `PreToolUse` keys
    are left in place even when empty, removing structure we did not create is
    the same overreach in the other direction.
    """
    if scope not in ("project", "user"):
        return {"ok": False, "error": f"unknown scope {scope!r}; use project|user"}
    settings_path = (Path.home() / ".claude" / "settings.json" if scope == "user"
                     else Path(project_dir) / ".claude" / "settings.json")
    if not settings_path.exists():
        return {"ok": True, "removed": 0, "path": str(settings_path),
                "note": "no settings file here, nothing was installed"}
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"ok": False,
                "error": f"{settings_path} is not valid JSON, refusing to "
                         "rewrite it; remove the bgate_cli.hook entry by hand"}
    groups = (settings.get("hooks") or {}).get("PreToolUse")
    if not isinstance(groups, list):
        return {"ok": True, "removed": 0, "path": str(settings_path),
                "note": "no PreToolUse hooks here, nothing to remove"}
    removed = 0
    kept_groups = []
    for group in groups:
        hooks = [h for h in (group.get("hooks") or [])
                 if "bgate_cli.hook" not in str(h.get("command", ""))]
        removed += len(group.get("hooks") or []) - len(hooks)
        if hooks:
            kept_groups.append({**group, "hooks": hooks})
        elif not (group.get("hooks") or []):
            kept_groups.append(group)      # somebody else's empty group
    if not removed:
        return {"ok": True, "removed": 0, "path": str(settings_path),
                "note": "the bgate hook was not installed here"}
    settings["hooks"]["PreToolUse"] = kept_groups
    settings_path.write_text(json.dumps(settings, indent=2) + "\n",
                             encoding="utf-8")
    return {"ok": True, "removed": removed, "path": str(settings_path),
            "note": "lane and lock enforcement is OFF here now, agents can "
                    "write anywhere their tools reach"}


def install_hook(project_dir: str, scope: str = "project") -> dict:
    """Merge the enforcement hook into a settings.json.

    scope="project" writes <project>/.claude/settings.json, the committed,
    per-repo gate. scope="user" writes ~/.claude/settings.json ONCE and covers
    every Builders Gate project on the machine, including ones that do not exist
    yet.

    USER SCOPE WORKS BECAUSE THE HANDLER WAS ALWAYS PROJECT-AGNOSTIC. It never
    read an installed-at path: it resolves the project by walking up from the
    file being written (hook.py `db.resolve_root(target_path.parent)`) and
    returns ALLOW when that finds nothing, so a write outside any game project
    is untouched. The per-project install was therefore never enforcing
    anything the user-scope one cannot, it was only ever a per-repo switch, and
    a switch you must remember to flip in each new project is a switch that is
    off exactly when a fresh project needs it most.

    Merges rather than overwrites, a game project may already carry its own
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
                    "error": f"{settings_path} exists but is not valid JSON, "
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
    if report.get("enforcement"):
        print()
        print(report["enforcement"])
        print()
    for probe in report["probes"]:
        mark = "ok  " if probe.get("ok") else "FAIL"
        print(f"{mark}  {probe['probe']}: {probe.get('error') or probe.get('got')}")
    if report["recent_failures"]:
        print(f"\n{len(report['recent_failures'])} recent FAIL-OPEN event(s), "
              "writes went through unchecked:")
        for row in report["recent_failures"]:
            print(f"  {row.get('ts', '?')}  {row.get('detail', '')[:120]}")
    print()
    print(report.get("reason", ""))
    return 0 if report["enforcing"] else 1


def init_project(name: str, kind: str = "2d", dest: str = "", pitch: str = "",
                 force: bool = False, replace: bool = False,
                 without: str = "", engine: str = "", brief: str = "",
                 refs: list | None = None) -> int:
    """Create the project store AND a runnable game, then say where it landed.

    The first-run gap the audit named: the only way to make a project was an MCP
    session calling project_init, which never printed the directory it wrote to.
    One command, one absolute path on stdout, that path is the whole point, so
    it is printed even when the scaffold had nothing new to write.
    """
    from bgate_core.runtime import engines as _engines
    from bgate_core.store import project, scaffold
    from bgate_core.store.util import slugify

    engine = (engine or _engines.DEFAULT).strip().lower()
    if not _engines.known(engine):
        print(f"error: --engine must be one of "
              f"{'|'.join(_engines.names())}, got {engine!r}")
        return 2
    if not _engines.template_dir_name(engine):
        print(f"error: {_engines.label(engine)} projects are adopted, not "
              f"scaffolded, there is no template. Point bgate at an existing "
              f"one with `bgate adopt`.")
        return 2
    if kind not in scaffold.KINDS:
        print(f"error: --kind must be one of {'|'.join(scaffold.KINDS)}, got {kind!r}")
        return 2
    if not name.strip():
        print("error: a project needs a name, bgate init <name>")
        return 2

    # Default to a NEW directory under the cwd rather than the cwd itself: a
    # scaffolder that unpacks a game into whatever directory you happened to be
    # standing in is a data-loss bug wearing a feature's hat.
    root = Path(dest).expanduser().resolve() if dest else (
        Path.cwd() / slugify(name)).resolve()

    try:
        made = scaffold.new_project(root, name, kind=kind, force=force,
                                    replace=replace, engine=engine)
    except FileExistsError as exc:
        print(f"error: {exc}")
        return 1
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}")
        return 2

    project.init(root, name, pitch=pitch, engine=engine, dimension=kind)
    # A 2D project defaults the 3D pipeline OFF, cutout/sprite work never
    # opens Blender, and the default should match the kind just chosen. A WEB
    # project defaults it off at both dimensions: blender_* delivers .glb into
    # a Godot import pipeline that a web project does not have, so leaving the
    # module on would advertise 48 tools whose last step cannot land.
    # Re-enable any time in Settings > Modules; an explicit --without wins.
    if engine == "web" and "three_d" not in (without or ""):
        without = (without + ",three_d") if without else "three_d"
        print("3D pipeline switched off - blender delivery targets Godot; "
              "re-enable in Settings > Modules if you want it anyway")
    elif kind == "2d" and "three_d" not in (without or ""):
        without = (without + ",three_d") if without else "three_d"
        print("3D pipeline switched off for a 2D project - re-enable in "
              "Settings > Modules if you want Blender work here")
    off_note = _store_modules_off(root, without)
    if off_note:
        print(off_note)
    # The scaffold writes project.godot, scenes/ and scripts/ straight into
    # <root>, while the default seat lanes are written against <root>/game.
    # Left alone, the gameplay seat cannot write the very scripts this command
    # just created, and agents build a second tree under game/ that the engine
    # does not load. Point the lanes at what was actually laid down.
    try:
        from bgate_core.board import seats as _seats
        _seats.apply_layout(root)
    except Exception:
        pass

    kicked = _seed_kickoff(root, brief, refs)
    if kicked is None:
        return 1

    print(f"created {name} ({kind}), {len(made['files'])} files")
    # SAY WHAT WAS PROTECTED, or a careful run reads as a broken one. `force`
    # now fills in what is missing and leaves anything the user has edited
    # alone, so a top-up over a live project legitimately writes nothing -
    # and "created 0 files" with no further word looks like the command
    # failed rather than like it declined to overwrite someone's work.
    if made.get("note"):
        print(made["note"])
    for entry in made.get("skipped") or []:
        print(f"  kept your {entry['file']}, {entry['reason']}")
    for entry in made.get("replaced") or []:
        print(f"  replaced {entry['file']} (backup: {entry['backup']})")
    print(str(root))
    print()
    print("next:")
    print(f"  cd {root}")
    print("  bgate serve            open the dashboard on this project")
    print("  bgate doctor           check the toolchain (godot, blender, ...)")
    if kicked:
        print("  the kickoff is on the thread: the director's first turn is to "
              "read the brief and lay out the board")
    return 0


def _all_of(argv: list, flag: str) -> list:
    """Every value given for a repeatable flag, in order."""
    out = []
    for i, token in enumerate(argv):
        if token == flag and i + 1 < len(argv):
            out.append(argv[i + 1])
    return out


def _seed_kickoff(root, brief: str = "", refs: list | None = None):
    """The card's brief-and-screenshots start, from the command line.

    ``brief`` is a path to a text file, ``refs`` a list of image paths. Returns
    the seed report, ``{}`` when nothing was asked for, or None after printing
    an error (a brief file that does not exist is a typo to fix, not a project
    to create without it).
    """
    if not brief and not refs:
        return {}
    from bgate_core.design import kickoff as _kickoff
    text = ""
    if brief:
        try:
            text = Path(brief).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read --brief {brief}: {exc}")
            return None
    entries = []
    for image in refs or []:
        src = Path(image).expanduser()
        if not src.is_file():
            print(f"error: --ref {image} is not a file")
            return None
        entries.append({"name": src.stem, "path": str(src), "kind": "concept"})
    try:
        seeded = _kickoff.seed(root, text, entries, actor="human")
    except ValueError as exc:
        print(f"error: {exc}")
        return None
    if seeded.get("brief"):
        print(f"brief saved to {seeded['brief']} "
              f"(bible section #{seeded['brief_section']})")
    for pin in seeded.get("pinned") or []:
        print(f"pinned {pin['name']} ({pin['kind']})")
    for skip in seeded.get("skipped") or []:
        print(f"  not pinned {skip['name']}: {skip['why']}")
    return seeded


def _store_modules_off(root, without: str) -> str:
    """Store ``--without floor,music`` module choices. Returns a note to print.

    The CLI half of the first-run card's checklist: the same
    ``modules.disabled`` setting, written at creation so the very first
    dashboard open and the very first agent session already respect it.
    Unknown names warn and are dropped, a typo must not silently disable the
    nearest real feature.
    """
    from bgate_core.store import modules, settings

    names = [w.strip() for w in (without or "").split(",") if w.strip()]
    known = [n for n in names if n in modules.MODULES]
    unknown = [n for n in names if n not in modules.MODULES]
    # The machine defaults, the setup wizard's component page, seed every
    # new project; --without adds to them for this one.
    seeded = sorted(modules.machine_defaults() | set(known))
    lines = []
    if unknown:
        lines.append(f"warning: no module named {', '.join(unknown)}, "
                     f"modules are: {', '.join(modules.names())}")
    if seeded:
        try:
            settings.set(root, "modules.disabled", seeded)
            lines.append("switched off: " + ", ".join(seeded)
                         + "  (change later in Settings > Modules)")
        except Exception as exc:
            lines.append(f"warning: could not store module choices ({exc}), "
                         "set modules.disabled in Settings instead")
    return "\n".join(lines)


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f}MB"


def adopt_project(directory: str = "", name: str = "", pitch: str = "",
                  kind: str = "", as_json: bool = False,
                  without: str = "", brief: str = "",
                  refs: list | None = None) -> int:
    """Adopt an EXISTING game and print what we understood about it.

    The printout is not decoration. The person running this has months of work
    in the directory and is being asked to trust a tool that just wrote to it;
    showing that we found their project.godot, counted their scenes and got the
    dimension right is the only evidence available at this point that we read
    the project rather than replaced it.
    """
    from bgate_core.store import adopt as _adopt
    from bgate_core.store import project

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

    off_note = _store_modules_off(target, without)
    kicked = _seed_kickoff(target, brief, refs)
    if kicked is None:
        return 1
    if kicked:
        report["kickoff"] = kicked

    if as_json:
        print(json.dumps(report, indent=2))
        return 0

    found = report["detected"]
    proj = report["project"]
    verb = "re-adopted" if report["already_adopted"] else "adopted"
    if off_note:
        print(off_note)
    print(f"{verb} {proj['name']}, {report['path']}")
    print()
    if found["godot"]:
        version = f" {found['godot_version']}" if found["godot_version"] else ""
        print(f"  godot{version}      {found['godot_dir']}")
        if found["main_scene"]:
            print(f"  main scene    {found['main_scene']}")
    elif found.get("engine"):
        # A game in another engine. Say which, and whether Builders Gate can
        # drive it, instead of reporting the absence of Godot as the finding.
        label = found.get("engine_label") or found["engine"]
        print(f"  engine        {label}  {found.get('engine_dir') or ''}")
        if found.get("engine_supported"):
            print(f"                recorded as '{found['engine']}'; its engine "
                  "tools register in place of the godot_* ones.")
        else:
            print(f"                recorded as '{found['engine']}'. Detected "
                  "and named, not driven: the board, canon and")
            print("                art pipeline work; no tool edits or runs "
                  "the game.")
    else:
        print("  engine        NOT FOUND: no project.godot, package.json or "
              "Unity ProjectSettings at or")
        print("                under this directory, so engine was recorded as "
              "'none' and every")
        print("                engine tool stays unavailable until one exists.")
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
    # THE LANES, WHEN THEY HAD TO MOVE. The default seat table is written
    # against <root>/game; a repo laid out any other way has no seat owning its
    # source tree, and every dispatched agent is refused on contact with it.
    # Say so, a silent remap is a surprise the first time someone reads
    # seat_list and finds globs they did not write.
    lanes = report.get("lanes") or {}
    if lanes.get("changed"):
        print(f"  relaned   seats re-rooted at "
              f"{lanes.get('prefix') or 'the project root'} "
              "(the default lanes assume <root>/game)")
    print()
    if not proj.get("pitch"):
        print("no pitch recorded, the bible starts empty without one. Set it:")
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
    from bgate_core.store import project

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
    from bgate_core.store import db, project

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
    from bgate_core.store import project

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
        print(f"error: nothing to serve, {root} has no index.html "
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
            # or the response carries two of them, the base handler already
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
    a game, no Godot project, hidden, export failed, looks identical to a
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
    wedged, or not running at all, while the agents it spawned very much are.
    The pid ledger lives in the project (``.bgate/agents/``), so this works
    against a dashboard that is already gone.

    Turns auto-deploy off first (otherwise the loop dispatches a replacement
    into the gap), kills each agent's whole process tree, reaps anything the
    ledger knows about, and settles the items so the board stops claiming work
    is running. Exit 0 even when nothing was running, this is the command you
    hammer, and "nothing to kill" is a success.
    """
    from bgate_ui.agents import dispatch as _dispatch

    root = project_dir or os.environ.get("BGATE_ROOT") or ""
    if not root:
        try:
            from bgate_core.store import project
            root = str(project.require_root())
        except Exception:
            print("no project here, run this inside a game project, "
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
        print("auto-deploy is now OFF, turn it back on from the console")
    settled = result.get("settled") or []
    if settled:
        print(f"settled {len(settled)} item(s) that were stuck 'dispatched'")
    for problem in result.get("errors") or []:
        print(f"  ! {problem}")
    if not stopped and not orphans:
        print("nothing was running.")
    return 0


def connect(targets: list[str], *, check: bool = False, show: bool = False,
            remove: bool = False, as_json: bool = False) -> int:
    """Wire your coding agent to the Builders Gate MCP server, or say why not.

    THE COMMAND THAT USED TO BE A PARAGRAPH IN THE README. Setup told the user
    to type

        claude mcp add builders-gate --scope user -- <ABSOLUTE-python-path> -m bgate_mcp.server

    and then explained, correctly, that getting the interpreter wrong produces
    a registration that looks fine, fails at the first tool call, and reports
    "failed to connect" pointing nowhere near the cause. Asking a new user to
    hand-assemble the one argument the docs admit is the most common failure on
    the supported platform is the papercut; this fills it in for them, because
    the interpreter it should name is the one running this process.

    Dashboard parity is deliberate: Settings → Agent CLIs does exactly this,
    through the same :mod:`bgate_ui.agentcli`. Nobody should have to start a
    server to finish an install, and nobody should get a different answer from
    the two places.

    WITH NO ARGUMENT IT WRITES NOTHING. A bare `bgate connect` is the report -
    every client, whether it is installed, and whether its registration is the
    good one. Registering is a named target or `--all`, because it changes what
    every future session of that client can do on this whole machine.
    """
    try:
        from bgate_ui.agents import agentcli
    except Exception as exc:                                     # noqa: BLE001
        print(f"error: the wiring registry is unavailable: {exc}")
        return 1

    known = agentcli.ids()
    unknown = [t for t in targets if t not in known]
    if unknown:
        print(f"error: unknown client(s): {', '.join(unknown)}")
        print(f"known: {', '.join(known)}")
        return 1

    applied: dict[str, dict] = {}
    if remove:
        for one in targets:
            applied[one] = agentcli.unregister(one)
    elif targets:
        for one in targets:
            got = agentcli.register(one)
            # VERIFY EVERY WRITE, because "registered" is precisely the claim
            # that has been wrong before. The interpreter the config now names
            # is asked whether it can import the server; that is the difference
            # between a registration and a working one.
            if got.get("ok"):
                got["verified"] = agentcli.verify(one)
            applied[one] = got
    elif check:
        for one in known:
            row = [r for r in agentcli.status() if r["id"] == one][0]
            if row["mcp"].get("found"):
                applied[one] = agentcli.verify(one)

    data = {**agentcli.payload(), "applied": applied}
    if as_json:
        print(json.dumps(data, indent=2))
    else:
        _print_connect(data, show=show)
    rows = data["runners"]
    if applied:
        return 0 if all(v.get("ok") for v in applied.values()) else 1
    # A bare report exits 0 when ANY client is correctly wired. Zero is the
    # honest failure: nothing on this machine can call the tools.
    return 0 if any(r["installed"] and r["mcp"].get("ok") for r in rows) else 1


def _print_connect(data: dict, *, show: bool = False) -> None:
    rows = data["runners"]
    applied = data.get("applied") or {}
    print(f"MCP server : {data['server']}")
    print(f"interpreter: {data['interpreter']}")
    print()
    width = max(len(r["label"]) for r in rows)
    for row in rows:
        mcp = row["mcp"]
        if not row["installed"]:
            mark, detail = "----", "not found on this machine"
            if row["install_hint"]:
                detail += f"  ({row['install_hint']})"
        elif mcp.get("ok"):
            mark, detail = "ok  ", "wired, pinned to this interpreter"
        else:
            mark, detail = "MISS", mcp.get("verdict", "")
        print(f"{mark}  {row['label'].ljust(width)}  {detail}")
        got = applied.get(row["id"])
        if got:
            note = got.get("error") or got.get("verdict") or got.get("output") or ""
            print(f"      {' ' * width}  -> {note.strip()[:400]}")
            checked = got.get("verified") or {}
            if checked:
                print(f"      {' ' * width}  -> "
                      + (checked.get("detail") or checked.get("error") or ""))
    print()
    todo = [r for r in rows if r["installed"] and not r["mcp"].get("ok")]
    writable = [r["id"] for r in todo if r["mcp"].get("can_register")]
    manual = [r for r in todo if not r["mcp"].get("can_register")]
    if writable:
        print(f"to wire them:  bgate connect {' '.join(writable)}")
    for row in manual:
        # NO BUTTON AND NO COMMAND, ON PURPOSE: these clients keep their MCP
        # servers in a JSON file the human also hand-edits, and this tool does
        # not merge into one. The block is printed with the interpreter already
        # correct, which is the part that goes wrong.
        print(f"{row['label']}: add this to {row['mcp']['config_path']}")
        if show:
            print(_indent(row["mcp"].get("block") or ""))
        else:
            print("      (re-run with --show to print the block)")
    if show:
        print()
        print("any other MCP client:")
        print(_indent(data.get("generic_block") or ""))
    print()
    print("restart the client after wiring it, a running session does not "
          "pick up a new MCP server.")


def _indent(text: str, pad: str = "      ") -> str:
    return "\n".join(pad + line for line in (text or "").splitlines())


def doctor(project_dir: str = "", as_json: bool = False) -> int:
    """Print the dependency report. Exit 1 if anything is unavailable.

    The exit code is the point: this is meant to be the one line a setup script
    or a CI step runs to decide whether the toolchain is usable, without
    grepping five status commands' output for the word "not found".
    """
    from bgate_core.runtime import doctor as _doctor

    root = project_dir or os.environ.get("BGATE_ROOT") or ""
    if not root:
        try:  # a cwd inside a project is the common case; not being in one is fine
            from bgate_core.store import project
            root = str(project.require_root())
        except Exception:
            root = ""

    report = _doctor.check(root or None, refresh=True)
    if as_json:
        # Nested under its own key, not merged: the top level of this document is
        # one row per dependency and a consumer that iterates it would read
        # "settings" as a missing binary.
        print(json.dumps({**report,
                          "settings": _doctor.settings_report(root or None),
                          "project": _doctor.project_report(root or None)},
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
        # WHICH ROWS ACTUALLY BLOCK YOU. This command exits 1 when anything at
        # all is missing, including things nobody needs on day one, so the
        # exit code has to be read alongside a sentence saying what the core
        # loop requires.
        core = [n for n in ("python", "godot")
                if not report.get(n, {}).get("available")]
        print("core loop needs python + godot" + (
            f", MISSING: {', '.join(core)}" if core else ": both present")
            + ". blender (3D), ffmpeg/ffprobe (video), whisper (voice) and "
              "an art key (image generation) are optional, a red row there "
              "is not a blocker.")
        # Project-level faults: lanes that match no directory here, and a hook
        # that was never installed. Neither is a missing binary, so neither can
        # appear above, and both stop agents dead.
        project_rows = _doctor.project_report(root or None)
        if project_rows:
            print()
            for row in project_rows:
                mark = "ok  " if row["ok"] else "WARN"
                print(f"{mark}  {row['name']}  {row['detail']}")
                if row["fix"]:
                    print(f"      fix: {row['fix']}")
        # The other half of "why is this board not doing what I told it": an env
        # var in a shell profile silently winning over what the panel shows.
        # AFTER the summary and deliberately outside the exit code, a setting
        # that is merely non-default is not a missing dependency.
        print()
        _doctor.print_settings(root or None)
    return 0 if all(row["available"] for row in report.values()) else 1


def _key_root(project_dir: str = "") -> str:
    """The project this key command is about, or "" when there is none.

    Not being in a project is a normal state here and not an error: the whole
    point of the machine-wide store is that a credential can be set and read
    with no game in sight.
    """
    root = project_dir or os.environ.get("BGATE_ROOT") or ""
    if root:
        return root
    try:
        from bgate_core.store import project
        return str(project.require_root())
    except Exception:
        return ""


def keys(action: str = "", provider_id: str = "", project_dir: str = "",
         use_global: bool = False, as_json: bool = False) -> int:
    """Show, set or clear provider credentials.

    THE KEY IS NEVER AN ARGUMENT. It is prompted for and read with echo off, so
    it does not land in shell history, in a process list, or in the scrollback
    of whoever is watching the screen. The project's own setup notes have said
    "never put one on a command line" since a key was committed once; a
    convenience flag here would be that rule with an exception carved into it.
    """
    from bgate_core.runtime import providers as _providers

    root = _key_root(project_dir)
    scope = "global" if use_global else "project"
    action = (action or "list").strip().lower()

    if action in ("", "list", "status", "show"):
        rows = _providers.status(root or None)
        if as_json:
            print(json.dumps({"project": root, "global_env":
                              str(_providers.envfile.global_path()),
                              "providers": rows}, indent=2))
            return 0
        width = max(len(row["id"]) for row in rows)
        # `source` is the column that answers the only hard question here -
        # which of three layers is actually supplying the value, so it is
        # spelled out rather than abbreviated to a tick.
        where = {"env_file": "project .env", "global_file": "~/.bgate/.env",
                 "environment": "shell", "shadowed": "SHADOWED", "unset": "-"}
        for row in rows:
            mark = "ok  " if row["available"] else ("set " if row["configured"]
                                                    else "MISS")
            tail = f"...{row['last4']}" if row["last4"] else ""
            note = "" if row["available"] else f"  {row['reason']}"
            print(f"{mark}  {row['id'].ljust(width)}  "
                  f"{where.get(row['source'], row['source']).ljust(14)} "
                  f"{tail}{note}")
        print()
        print(f"project: {root or '(none, the machine-wide store still applies)'}")
        print(f"global:  {_providers.envfile.global_path()}")
        return 0

    if action not in ("set", "clear"):
        print(f"unknown key action {action!r}, set, clear, or list")
        return 2
    if not provider_id:
        print("which provider? one of: " + ", ".join(_providers.ids()))
        return 2

    try:
        if action == "clear":
            row = _providers.clear_key(root or None, provider_id, scope=scope,
                                       actor="cli")
        else:
            import getpass

            one = _providers.by_id(provider_id)
            print(f"{one.label}, {one.key_url}")
            value = getpass.getpass(f"paste {one.env} (input hidden): ").strip()
            if not value:
                print("nothing pasted; no change")
                return 2
            row = _providers.set_key(root or None, provider_id, value,
                                     scope=scope, actor="cli")
    except _providers.ProviderError as exc:
        print(str(exc))
        return 2
    except OSError as exc:
        print(f"could not write the {scope} .env: {type(exc).__name__}: {exc}")
        return 1

    if as_json:
        print(json.dumps(row, indent=2))
        return 0
    target = (_providers.envfile.global_path() if scope == "global"
              else Path(root) / ".env")
    print(f"{row['write']}  {row['env']}  in {target}")
    if row.get("gitignore"):
        print(f"  .gitignore: {row['gitignore']}")
    if not row["available"] and row["reason"]:
        print(f"  still unusable: {row['reason']}")
    elif row["configured"] and row["source"] == "env_file" and use_global:
        # The write landed and something else is still winning. Saying so here
        # is the difference between "it did not work" and "it worked, and this
        # project overrides it".
        print("  note: this project's own .env still supplies this key, so the "
              "global one applies everywhere else")
    return 0


def _writable_console() -> None:
    """Stop the Windows console mangling our own prose.

    Reason strings carry em dashes like every other string in this codebase, and
    a stock Windows console is cp1252, so `bgate doctor` printed its advice as
    mojibake, which is a poor first impression for the command people run when
    something is already wrong. Best-effort: a stream that cannot be
    reconfigured is left alone rather than crashing the CLI over punctuation.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _cmd_animlib(rest: list) -> int:
    """bgate animlib [status | list <pack> | fetch <pack> [--force]]

    THE ONE DOWNLOAD IN THE 3D PATH, and it lives in the CLI on purpose: a
    commit-pinned zip, SHA-256 checked before a byte is unpacked, into
    ~/.bgate/animlib where every project shares it. No MCP tool fetches -
    the same rule that keeps key-writing out of an agent's hands.
    """
    from bgate_adapters import animlib
    sub_cmd = rest[0] if rest else "status"
    if sub_cmd == "status":
        st = animlib.status()
        print(f"animation libraries in {st['home']}")
        for name, row in st["packs"].items():
            mark = "OK  " if row["fetched"] else "MISS"
            tail = (f"{row.get('clips', 0)} clips" if row["fetched"]
                    else f"run: {row['fetch']}")
            print(f"  {mark} {name:<18} {row['license']:<9} {tail}")
        return 0
    if sub_cmd == "list":
        if len(rest) < 2:
            print("bgate animlib list <pack>")
            return 2
        try:
            rows = animlib.clips(rest[1])
        except FileNotFoundError as exc:
            print(exc)
            return 1
        for c in rows:
            flags = ("loop" if c["loop"] else "") + (" root-motion" if c["root_motion"] else "")
            print(f"  {c['name']:<28} {c['seconds']:>6.2f}s  {flags}")
        return 0
    if sub_cmd == "fetch":
        if len(rest) < 2:
            print("bgate animlib fetch <pack> [--force]")
            return 2
        got = animlib.fetch(rest[1], force="--force" in rest)
        if not got.get("ok"):
            print(f"FAILED: {got.get('error')}")
            return 1
        if got.get("fetched"):
            print(f"fetched {got['pack']}: {got['clips']} clips, "
                  f"{got['bytes']} bytes, sha256 {got['sha256'][:12]}")
        else:
            print(f"{got['pack']} already fetched at {got['path']}")
        return 0
    print("bgate animlib [status | list <pack> | fetch <pack> [--force]]")
    return 2


def _reuse_root(rest: list):
    """The project a kit or library command works in: --project DIR, else the
    enclosing or active project. Returns (root, game_dir)."""
    from bgate_core.store import project as _project
    start = None
    if "--project" in rest:
        i = rest.index("--project") + 1
        if i < len(rest):
            start = rest[i]
    root = _project.require_root(start)
    game = _project.game_dir(root, _project.engine_of(root)) or root
    return root, game


_REUSE_VALUE_FLAGS = ("--project", "--tags", "--collection", "--dest", "--note",
                      "--kind")


def _reuse_positional(rest: list) -> list:
    skip: set[int] = set()
    for i, token in enumerate(rest):
        if token in _REUSE_VALUE_FLAGS:
            skip.update({i, i + 1})
    return [a for i, a in enumerate(rest)
            if i not in skip and not a.startswith("-")]


def _reuse_opt(rest: list, flag: str, default: str = "") -> str:
    if flag in rest:
        i = rest.index(flag) + 1
        if i < len(rest):
            return rest[i]
    return default


def _cmd_kit(rest: list) -> int:
    """bgate kit [list | install NAME [--force] [--no-bind] | remove NAME [--force]] [--project DIR]

    The same install kit_install performs, from the shell: a kit's scripts
    into scripts/, its missing input actions into project.godot, nothing
    overwritten. `list` says which are already in this game; --all lists
    kits of every dimension, not only the project's.
    """
    from bgate_core.store import kits as _kits
    from bgate_core.store import project as _project
    sub = rest[0] if rest and not rest[0].startswith("-") else "list"
    positional = _reuse_positional(rest)
    try:
        root, game = _reuse_root(rest)
    except Exception as exc:                                      # noqa: BLE001
        print(f"error: {exc}")
        return 1
    dimension = ""
    try:
        dimension = _project.get(root).get("dimension") or ""
    except Exception:                                             # noqa: BLE001
        pass
    if "--all" in rest:
        dimension = ""
    if sub == "list":
        st = _kits.status(root, game, "godot", dimension)
        print(f"kits for {game} ({st['dimension']})")
        for k in st["kits"]:
            if k.get("error"):
                print(f"  BROKEN {k['name']:<24} {k['error']}")
                continue
            mark = {"installed": "OK  ", "absent": "    ", "modified": "MOD ",
                    "partial": "PART"}.get(k["state"], "??  ")
            stale = " (stale: the kit changed since install)" if k.get("stale") else ""
            print(f"  {mark} {k['name']:<24} {k['dimension']:<4} {k['title']}{stale}")
        return 0
    if sub in ("install", "add"):
        if len(positional) < 2:
            print("bgate kit install NAME [--force] [--no-bind] [--all] [--project DIR]")
            return 2
        try:
            got = _kits.install(root, game, positional[1], force="--force" in rest,
                                bind="--no-bind" not in rest, dimension=dimension)
        except _kits.KitError as exc:
            print(f"error: {exc}")
            return 1
        for rel in got["written"]:
            print(f"  wrote   {rel}")
        for row in got["kept"]:
            print(f"  kept    {row['dest']}: {row['reason']}")
        for row in got["replaced"]:
            print(f"  backup  {row['dest']} -> {row['backup']}")
        if got["actions_added"]:
            print(f"  actions {', '.join(got['actions_added'])} added to project.godot")
        if got["actions_missing"]:
            print(f"  MISSING actions {', '.join(got['actions_missing'])} (--no-bind)")
        if got["autoloads_missing"]:
            print(f"  MISSING autoloads {', '.join(got['autoloads_missing'])}")
        if got["note"]:
            print(f"  {got['note']}")
        print()
        print(got["usage"])
        return 0 if got["ok"] else 1
    if sub in ("remove", "rm"):
        if len(positional) < 2:
            print("bgate kit remove NAME [--force] [--project DIR]")
            return 2
        try:
            got = _kits.remove(root, game, positional[1], force="--force" in rest)
        except _kits.KitError as exc:
            print(f"error: {exc}")
            return 1
        for rel in got["removed"]:
            print(f"  removed {rel}")
        for row in got["refused"]:
            print(f"  REFUSED {row['dest']}: {row['reason']}")
        return 0 if got["ok"] else 1
    print(_cmd_kit.__doc__.splitlines()[0])
    return 2


def _cmd_library(rest: list) -> int:
    """bgate library [list | search TEXT | publish PATH... | import ID... | forget ID] [--project DIR]

    The machine-wide asset library in ~/.bgate/library. `forget` lives HERE
    and not as an MCP tool on purpose: a store every project shares must not
    be emptiable by an agent.
    """
    from bgate_core.store import assetlib as _assetlib
    sub = rest[0] if rest and not rest[0].startswith("-") else "list"
    positional = _reuse_positional(rest)

    def show(entries):
        for e in entries:
            dims = f"{e['dims'][0]}x{e['dims'][1]}" if e.get("dims") else ""
            tags = ",".join(e.get("tags") or [])
            coll = e.get("collection") or ""
            print(f"  {e['id']}  {e['kind']:<9} {e['name']:<32} {dims:<9} "
                  f"{coll:<14} {tags}")

    if sub in ("list", "search"):
        query = " ".join(positional[1:]) if sub == "search" else ""
        got = _assetlib.search(query, kind=_reuse_opt(rest, "--kind"),
                               collection=_reuse_opt(rest, "--collection"),
                               tags=[t for t in _reuse_opt(rest, "--tags").split(",") if t],
                               limit=200)
        print(f"library at {got['home']}: {got['library_size']} entries, "
              f"{got['matched']} match")
        show(got["entries"])
        return 0
    if sub == "publish":
        if len(positional) < 2:
            print("bgate library publish PATH... [--tags a,b] [--collection C] "
                  "[--note TEXT] [--project DIR]")
            return 2
        try:
            root, _game = _reuse_root(rest)
            from bgate_core.store import project as _project
            try:
                name = _project.get(root).get("name") or ""
            except Exception:                                     # noqa: BLE001
                name = ""
            got = _assetlib.publish(
                root, positional[1:],
                tags=[t for t in _reuse_opt(rest, "--tags").split(",") if t],
                collection=_reuse_opt(rest, "--collection"),
                note=_reuse_opt(rest, "--note"), project_name=name)
        except Exception as exc:                                  # noqa: BLE001
            print(f"error: {exc}")
            return 1
        print(f"published {len(got['published'])} new, "
              f"{len(got['existing'])} already held")
        show(got["published"] + got["existing"])
        return 0
    if sub == "import":
        if len(positional) < 2:
            print("bgate library import ID... [--dest DIR] [--overwrite] [--project DIR]")
            return 2
        try:
            _root, game = _reuse_root(rest)
            got = _assetlib.import_entries(
                game, positional[1:],
                dest=_reuse_opt(rest, "--dest", "assets/library"),
                overwrite="--overwrite" in rest)
        except Exception as exc:                                  # noqa: BLE001
            print(f"error: {exc}")
            return 1
        for row in got["imported"]:
            verb = "landed " if row.get("landed") else "present"
            print(f"  {verb} {row['path']}")
        for row in got["refused"]:
            print(f"  REFUSED {row['id']}: {row['reason']}")
        return 0 if got["ok"] else 1
    if sub == "forget":
        if len(positional) < 2:
            print("bgate library forget ID")
            return 2
        try:
            got = _assetlib.forget(positional[1])
        except _assetlib.LibraryError as exc:
            print(f"error: {exc}")
            return 1
        print(f"forgot {got['forgot']} ({got['name']})"
              + ("" if got["blob_removed"]
                 else "; bytes kept, another entry shares them"))
        return 0
    print(_cmd_library.__doc__.splitlines()[0])
    return 2


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

        flagged = {"--kind", "--dir", "--pitch", "--without", "--engine",
                   "--brief", "--ref"}
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
                            brief=opt("--brief"), refs=_all_of(rest, "--ref"),
                            # --replace is the only way to overwrite from the
                            # command line. --force stopped meaning that when it
                            # was found destroying customised files in place -
                            # export_presets.cfg is gitignored by the template
                            # this ships, so for anyone with custom export
                            # targets it was unrecoverable. Deliberate
                            # replacement is still legitimate; it just has to be
                            # asked for, and it takes a .bak first.
                            force="--force" in rest or "--replace" in rest,
                            replace="--replace" in rest,
                            without=opt("--without"),
                            engine=opt("--engine"))

    if cmd == "adopt":
        rest = args[1:]

        def opt(flag: str, default: str = "") -> str:
            if flag in rest:
                index = rest.index(flag) + 1
                if index < len(rest):
                    return rest[index]
            return default

        flagged = {"--name", "--pitch", "--kind", "--without", "--brief", "--ref"}
        skip: set[int] = set()
        for i, token in enumerate(rest):
            if token in flagged:
                skip.update({i, i + 1})
        positional = [a for i, a in enumerate(rest)
                      if i not in skip and not a.startswith("-")]
        return adopt_project(positional[0] if positional else "",
                             name=opt("--name"), pitch=opt("--pitch"),
                             kind=opt("--kind"), as_json="--json" in rest,
                             without=opt("--without"), brief=opt("--brief"),
                             refs=_all_of(rest, "--ref"))

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

    if cmd == "animlib":
        return _cmd_animlib(args[1:])

    if cmd in ("kit", "kits"):
        return _cmd_kit(args[1:])

    if cmd in ("library", "lib"):
        return _cmd_library(args[1:])

    if cmd == "doctor":
        positional = [a for a in args[1:] if not a.startswith("-")]
        return doctor(positional[0] if positional else "", as_json="--json" in args)

    if cmd in ("connect", "wire"):
        rest = args[1:]
        positional = [a.lower() for a in rest if not a.startswith("-")]
        if "--all" in rest:
            # --all means every client we can WRITE to, not every client we can
            # name: a file-kind row has no write, and listing it here would put
            # a guaranteed failure in the exit code of a command the user was
            # told to run.
            from bgate_ui.agents import agentcli as _ac
            positional = [r["id"] for r in _ac.status()
                          if r["installed"] and r["mcp"].get("can_register")]
        return connect(positional,
                       check="--check" in rest,
                       show="--show" in rest,
                       remove="--remove" in rest,
                       as_json="--json" in rest)

    if cmd in ("key", "keys"):
        rest = args[1:]
        positional = [a for a in rest if not a.startswith("-")]
        action = positional[0] if positional else "list"
        # `bgate key openai` reads as a request about that provider, not as a
        # bad verb, accept it as a listing rather than a usage error.
        if action not in ("list", "status", "show", "set", "clear"):
            action, provider_id = "list", ""
            directory = ""
        else:
            provider_id = positional[1] if len(positional) > 1 else ""
            directory = positional[2] if len(positional) > 2 else ""
        return keys(action, provider_id, directory,
                    use_global=("--global" in rest or "-g" in rest),
                    as_json="--json" in rest)

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

    if cmd == "un-adopt":
        positional = [a for a in args[1:] if not a.startswith("-")]
        target = Path(positional[0] if positional else ".").expanduser().resolve()
        marker = target / ".bgate"
        if not marker.is_dir():
            print(f"{target} is not an adopted project, nothing to undo")
            return 1
        if "--yes" not in args:
            print(f"This will DELETE {marker}, the board, the bible, the lore, "
                  "and the artifact ledger for this project.")
            print("Your game files are untouched, and so are the marked blocks "
                  "in .gitignore and CLAUDE.md (delete those by hand if you "
                  "want them gone, they are marker-delimited).")
            print(f"Re-run with --yes to confirm:  bgate un-adopt {target} --yes")
            return 1
        import shutil
        try:
            from bgate_core.store import db as _db
            _db.close_all()
        except Exception:
            pass
        try:
            shutil.rmtree(marker)
        except OSError as exc:
            print(f"error: could not remove {marker}: {exc}")
            return 1
        print(f"removed {marker}")
        print("the game itself is untouched. `bgate hook-uninstall` if you also "
              "want the lane hook out of this repo's .claude/settings.json.")
        return 0

    if cmd == "hook-uninstall":
        rest = args[1:]
        scope = "project"
        if "--scope" in rest:
            i = rest.index("--scope")
            scope = rest[i + 1] if i + 1 < len(rest) else ""
            rest = rest[:i] + rest[i + 2:]
        positional = [a for a in rest if not a.startswith("-")]
        target = "." if scope == "user" else (positional[0] if positional else ".")
        result = uninstall_hook(target, scope=scope)
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
        remote = "--remote" in args
        from bgate_ui.app import serve
        serve(port=port, remote=remote)
        return 0

    if cmd == "app":
        port = None
        if "--port" in args:
            port = int(args[args.index("--port") + 1])
        from bgate_ui.window.desktop import run as run_desktop
        return run_desktop(port=port, debug="--debug" in args,
                           remote="--remote" in args)

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
