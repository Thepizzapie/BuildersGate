"""bgate — the console entrypoint.

    bgate init NAME [--kind 2d|3d] [--dir DIR] [--pitch TEXT] [--force]
                                create a project + a runnable game, and print where
    bgate serve [--port 7788]   run the dashboard
    bgate doctor [DIR] [--json] check every external dependency in one pass
    bgate hook-install [DIR]    wire lane/lock enforcement into a game project
    bgate hook                  (internal) the PreToolUse hook itself
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOOK_CONFIG = {
    "matcher": "Write|Edit|MultiEdit|NotebookEdit",
    "hooks": [{"type": "command",
               "command": sys.executable.replace("\\", "/") + " -m bgate_cli.hook"}],
}


def install_hook(project_dir: str) -> dict:
    """Merge the enforcement hook into <project>/.claude/settings.json.

    Merges rather than overwrites — a game project may already carry its own
    hooks, and clobbering them is exactly the kind of stomp this tool polices.
    """
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
    pre = hooks.setdefault("PreToolUse", [])
    already = any(
        h.get("command", "").endswith("bgate_cli.hook")
        for entry in pre for h in entry.get("hooks", [])
    )
    if not already:
        pre.append(HOOK_CONFIG)
        settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "settings": str(settings_path),
        "installed": not already,
        "note": "set BGATE_SEAT=<role> in the session's environment to enforce; "
                "without it the hook is inert",
    }


def init_project(name: str, kind: str = "2d", dest: str = "", pitch: str = "",
                 force: bool = False) -> int:
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
        made = scaffold.new_project(root, name, kind=kind, force=force)
    except FileExistsError as exc:
        print(f"error: {exc}")
        return 1
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}")
        return 2

    project.init(root, name, pitch=pitch, engine="godot", dimension=kind)

    print(f"created {name} ({kind}) — {len(made['files'])} files")
    print(str(root))
    print()
    print("next:")
    print(f"  cd {root}")
    print("  bgate serve            open the dashboard on this project")
    print("  bgate doctor           check the toolchain (godot, blender, ...)")
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
        print(json.dumps(report, indent=2))
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
                            force="--force" in rest)

    if cmd == "doctor":
        positional = [a for a in args[1:] if not a.startswith("-")]
        return doctor(positional[0] if positional else "", as_json="--json" in args)

    if cmd == "hook":
        from bgate_cli.hook import main as hook_main
        return hook_main()

    if cmd == "hook-install":
        target = args[1] if len(args) > 1 else "."
        print(json.dumps(install_hook(target), indent=2))
        return 0

    if cmd == "serve":
        port = 7788
        if "--port" in args:
            port = int(args[args.index("--port") + 1])
        from bgate_ui.app import serve
        serve(port=port)
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
