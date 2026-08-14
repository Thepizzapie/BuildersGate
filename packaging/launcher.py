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


def _whisper(args: list[str]) -> int:
    """Run the bundled transcription runner, with its arguments checked.

    Signature, fixed by bgate_adapters.transcribe.runner_cmd:

        <wav> <model> <device> <compute_type> <language|->

    Everything is validated against a closed set before runpy sees it. This is
    a subcommand on a binary sitting in the user's Programs folder — anything
    running as them can call it — so it should accept exactly the shape its one
    caller sends and refuse the rest. Nothing here is a privilege boundary (the
    caller is already the user); it is about not having an entry point whose
    behaviour is "whatever argv says".
    """
    import runpy

    from bgate_adapters import transcribe

    if len(args) != 5:
        sys.stderr.write(
            "usage: BuildersGate.exe whisper <wav> <model> <device> "
            "<compute_type> <language|->\n")
        return 2

    wav, model, device, compute, language = args
    if model not in transcribe.MODELS:
        sys.stderr.write(f"model must be one of {transcribe.MODELS}\n")
        return 2
    if device not in ("auto", "cpu", "cuda"):
        sys.stderr.write("device must be auto, cpu or cuda\n")
        return 2
    if compute not in ("auto", "int8", "int8_float16", "float16", "float32"):
        sys.stderr.write("unknown compute type\n")
        return 2
    if not os.path.isfile(wav):
        sys.stderr.write(f"no such audio file: {wav}\n")
        return 2

    sys.argv = ["_whisper_runner.py", wav, model, device, compute, language]
    runpy.run_module("bgate_adapters._whisper_runner", run_name="__main__")
    return 0


def _selftest() -> int:
    """Import everything the WINDOW needs, open nothing, and report.

    THE SMOKE TEST IN build_exe.py ONLY EXERCISES `serve`. It boots the server
    and fetches real files, which catches a bundle that lost its data trees or
    a route module — but the desktop window is a different import graph
    entirely (pywebview, its WinForms/EdgeChromium backend, pythonnet, the
    WebView2 loader DLL), and none of it is touched by an HTTP request. A
    bundle can pass every smoke path and still have no window.

    That gap is not theoretical: bgate.spec EXCLUDES packages to keep the
    download small, and the pruning that removed `cryptography` was justified
    by reading pywebview's source to confirm only its ssl=True path imports it.
    A check that runs is worth more than a note saying somebody once read the
    source, so this asserts the conclusion instead.

    Deliberately does NOT create a window or start a GUI loop: this has to pass
    on a headless CI runner. Importing the backend module is what proves
    PyInstaller bundled it; instantiating it would prove something about the
    runner's desktop session instead.
    """
    import json

    report: dict = {"ok": True, "checks": {}}

    def check(name, fn):
        try:
            report["checks"][name] = {"ok": True, "detail": fn()}
        except Exception as exc:                                # noqa: BLE001
            report["checks"][name] = {"ok": False,
                                      "detail": f"{type(exc).__name__}: {exc}"}
            report["ok"] = False

    def _webview():
        # pywebview exposes no __version__ attribute, so the metadata is the
        # only place the version exists — and inside a frozen bundle it is
        # there only because PyInstaller collected the dist-info. Reported
        # rather than asserted: a "?" here is not a failure, it just means a
        # bug report will not say which pywebview this build carries.
        import webview                                          # noqa: F401
        try:
            from importlib.metadata import version
            return version("pywebview")
        except Exception:                                       # noqa: BLE001
            return "installed, version unknown"

    def _backend():
        # The Windows backend pywebview picks by platform detection. Named
        # explicitly in bgate.spec's hiddenimports because nothing imports it
        # statically — so an edit there that drops it fails HERE rather than on
        # a user's first double-click.
        import webview.platforms.edgechromium as _ec
        return _ec.__name__

    def _loader():
        # WebView2Loader.dll, which bgate_ui.webview2 loads by relative path.
        # Its absence costs the window silently: the app falls back to a
        # browser tab and nobody finds out until they read the docs.
        #
        # THE ARCHITECTURE IS THE POINT. pywebview vendors three copies —
        # win-x86, win-x64, win-arm64 — and the spec's glob collects whichever
        # of them exist. This build is x64, so a bundle carrying only the x86
        # loader would pass a mere "the file is present" check and still fail to
        # load at run time.
        import pathlib

        import webview
        base = pathlib.Path(webview.__file__).parent / "lib" / "runtimes"
        dlls = list(base.rglob("WebView2Loader.dll")) if base.is_dir() else []
        if not dlls:
            raise FileNotFoundError(f"no WebView2Loader.dll under {base}")
        # runtimes/<rid>/native/WebView2Loader.dll — the RID is two levels up
        # from the file, not three.
        def rid(p):
            return p.parent.parent.name

        x64 = [d for d in dlls if "x64" in rid(d)]
        if not x64:
            raise FileNotFoundError(
                "no win-x64 WebView2Loader.dll — found only "
                + ", ".join(sorted(rid(d) for d in dlls)))
        return str(x64[0])

    def _desktop():
        from bgate_ui import desktop
        return f"window {desktop.DEFAULT_SIZE[0]}x{desktop.DEFAULT_SIZE[1]}"

    def _tls():
        # The excludes drop `cryptography`; they must not have touched Python's
        # own TLS, which is what every outbound API call goes through.
        import ssl
        return ssl.OPENSSL_VERSION

    def _native():
        """The check that decides which window the app actually opens.

        bgate_ui.webview2 is the primary window on Windows and pywebview is only
        its fallback — and available() is the gate. It needs comtypes, which was
        declared by nothing until today, so in every shipped build it answered
        "comtypes is not installed", the app quietly used pywebview, and the
        whole native path (its icon handling, and the frameless title bar) was
        dead code nobody could see was dead. The fallback opens a window too,
        which is exactly why this went unnoticed.

        So it is asserted here rather than assumed: a bundle where this fails
        still runs, still opens a window, and is not the product.
        """
        from bgate_ui import webview2
        ok, why = webview2.available()
        if not ok:
            raise RuntimeError(f"native window unavailable: {why}")
        import comtypes
        return f"webview2 available, comtypes {comtypes.__version__}"

    def _audio():
        """The playtest recorder's audio stack.

        THE CHECK THAT WAS MISSING. sounddevice and numpy were both excluded
        from the bundle as "an optional extra", and the result was that the
        Playtests screen — a top-level destination in the app — could never
        record anything in a packaged build. It reported "record unavailable"
        and suggested a pip command, forever, and no test noticed because no
        test ever asked the frozen binary whether it could open a microphone.

        Imports only. Enumerating devices needs a machine with a sound card and
        this has to pass on a CI runner; what is being defended here is the
        BUNDLE's contents, not the runner's hardware.
        """
        import numpy
        import sounddevice                                      # noqa: F401
        from bgate_adapters import recorder                     # noqa: F401
        return f"sounddevice + numpy {numpy.__version__}"

    check("pywebview", _webview)
    check("native-window", _native)
    check("audio-capture", _audio)
    check("edgechromium-backend", _backend)
    check("webview2-loader", _loader)
    check("desktop-module", _desktop)
    check("tls", _tls)

    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


def _ensure_std_streams() -> None:
    """Give the process real stdout/stderr objects. THIS RUNS BEFORE ANYTHING.

    A windowed PyInstaller build (console=False) started from Explorer, a Start
    Menu shortcut or any launcher that does not hand it a console gets
    ``sys.stdout is None`` and ``sys.stderr is None``. Not a closed file — None.

    That is fatal here, and not in an obvious place: uvicorn's default logging
    config installs a ColourizedFormatter whose __init__ calls
    ``sys.stdout.isatty()`` to decide whether to emit colour. So the app died
    inside logging.config with

        AttributeError: 'NoneType' object has no attribute 'isatty'
        ValueError: Unable to configure formatter 'default'

    …before the server bound a port or the window opened. Both entry points hit
    it, because both build a uvicorn Config.

    THIS SURVIVED EVERY TEST because the tests always supplied what the real
    launch path does not: build_exe.py's smoke test spawns the exe with
    subprocess.PIPE, which gives the child a genuine stdout handle. A harness
    that hands the process a console cannot see a bug about not having one.
    smoke_detached() now boots it the way Windows does instead.

    os.devnull rather than a log file: with no console there is nobody to read
    the stream, and the two things worth keeping — the crash log and the
    message box — are written by main()'s own except block.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:
                pass


def main() -> int:
    _ensure_std_streams()

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

        if cmd == "selftest":
            return _selftest()

        if cmd == "whisper":
            # The app running its own speech-to-text runner as a subprocess.
            # A frozen build has no interpreter to hand a script path to, so
            # transcribe.runner_cmd() calls THIS binary with `whisper` and the
            # runner's ordinary argv. Kept out of process on purpose: a loaded
            # whisper model is hundreds of MB pinned for the life of whatever
            # holds it, and a transcription that cannot be killed is a
            # dashboard that cannot be closed.
            #
            # THE ARGUMENTS ARE CHECKED HERE, not just inside the runner. This
            # is an entry point on a shipped binary that anything on the
            # machine can invoke, and `runpy` executes a module with whatever
            # argv it is handed. Validating shape and membership before that
            # keeps the surface to "read this wav with one of five models",
            # which is all the caller in transcribe.py ever asks for.
            return _whisper(argv[1:])

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
            "  BuildersGate.exe selftest     check the bundle, open nothing\n"
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
