"""Entry point for the frozen Windows build.

Kept separate from `bgate_cli.main` on purpose: the CLI's job is to parse a
command line, and a double-clicked .exe has none. This opens the desktop window
and nothing else, while still honouring `BuildersGate.exe serve` for anyone who
wants the browser dashboard out of the same binary.
"""
from __future__ import annotations

import multiprocessing
import os
import sys


def main() -> int:
    # PyInstaller + anything that spawns a process: without this the child
    # re-runs the bundle from the top and you get an endless fan of windows.
    multiprocessing.freeze_support()

    argv = sys.argv[1:]

    def _port(default=None):
        if "--port" in argv:
            try:
                return int(argv[argv.index("--port") + 1])
            except (IndexError, ValueError):
                pass
        return default

    # A frozen app has no console to print a traceback into, so a crash before
    # the window opens is silent. Route it somewhere the user can find.
    try:
        cmd = argv[0] if argv and not argv[0].startswith("-") else ""

        if cmd == "serve":
            from bgate_ui.app import serve
            serve(port=_port(7788))
            return 0

        if cmd in ("", "app"):
            from bgate_ui.desktop import run
            return run(port=_port(), debug="--debug" in argv)

        # ── everything else opens NOTHING ──────────────────────────────────
        # This used to fall through to run(), so ANY argv the launcher did not
        # recognise opened a desktop window. In a frozen build sys.executable is
        # this .exe rather than python.exe, so anything that shells out to
        # "the current interpreter" — a version probe, a helper subprocess —
        # launches the whole app again. That is how a single /api/doctor call
        # put thirteen windows on screen.
        #
        # An unknown argument is now a usage error on stderr and a non-zero
        # exit. A GUI is only ever opened deliberately.
        sys.stderr.write(
            "Builders Gate\n"
            "  BuildersGate.exe              open the desktop window\n"
            "  BuildersGate.exe app          same, explicitly\n"
            "  BuildersGate.exe serve [--port N]   browser dashboard\n"
            f"\nunrecognised argument: {argv[0]!r}\n"
        )
        return 2
    except Exception:                                          # noqa: BLE001
        import traceback
        crash = os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
            "BuildersGate-crash.log",
        )
        try:
            with open(crash, "w", encoding="utf-8") as fh:
                traceback.print_exc(file=fh)
        except OSError:
            pass
        traceback.print_exc()
        # No console on a windowed build — say it in a dialog the user will see.
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                "Builders Gate could not start.\n\nDetails written to:\n" + crash,
                "Builders Gate", 0x10,
            )
        except Exception:                                      # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
