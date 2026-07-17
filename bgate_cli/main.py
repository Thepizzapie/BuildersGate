"""bgate — the console entrypoint.

    bgate serve [--port 7788]   run the dashboard
    bgate hook-install [DIR]    wire lane/lock enforcement into a game project
    bgate hook                  (internal) the PreToolUse hook itself
"""
from __future__ import annotations

import json
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


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "help"

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
