"""`bgate app` — the dashboard in a native window instead of a browser tab.

The dashboard is already a local-only web app talking to a local store over
loopback, so there is nothing to port: this starts the same uvicorn server on a
background thread and points a native webview at it. On Windows that webview is
Edge WebView2, which ships with Windows 11 — no runtime to install, no Node
toolchain, no second copy of Chromium in the wheel.

pywebview is an OPTIONAL dependency (`pip install "builders-gate[desktop]"`).
Everything here degrades to a clear instruction if it is missing, because the
browser dashboard is still a complete way to use the product.
"""
from __future__ import annotations

import socket
import sys
import threading
from typing import Optional

WINDOW_TITLE = "Builders Gate"
MIN_SIZE = (1100, 720)
DEFAULT_SIZE = (1480, 940)

# The rail collapses to icons at 1180px and to a top bar at 820px, so a window
# narrower than the icon-rail breakpoint is a layout nobody designed for.
_MIN_USABLE_WIDTH = 820


def _free_port() -> int:
    """Ask the OS for a port nobody is using.

    `bgate serve` hardcodes 7788 and fails outright when something already has
    it. A desktop window has no reason to care which port it got — nothing else
    needs to find it — so it takes whatever is free and avoids colliding with a
    dashboard the user already has open.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_server(port: int, timeout: float = 20.0) -> bool:
    """Block until the server accepts a connection, or give up.

    Without this the window opens on a connection-refused page and stays there:
    pywebview does not retry, so losing the race renders a permanent error page
    for a server that came up 200ms later.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.05)
    return False


# A loopback bind is the simplest single-instance lock that is actually
# reliable on Windows: the OS drops it when the process dies, so it cannot be
# left stale by a crash the way a lock FILE can. Held for the process lifetime.
_SINGLETON_PORT = 7787
_singleton_sock = None


def _claim_singleton() -> bool:
    """True if we are the first instance; False if one is already up.

    This exists because a frozen build re-launches itself far too easily —
    sys.executable is the .exe, so anything shelling out to "the interpreter"
    starts a whole new app. One stray call put thirteen windows on screen.
    """
    global _singleton_sock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # No SO_REUSEADDR on purpose — reuse is exactly what we are preventing.
        s.bind(("127.0.0.1", _SINGLETON_PORT))
        s.listen(1)
        _singleton_sock = s
        return True
    except OSError:
        s.close()
        return False


def run(port: Optional[int] = None, debug: bool = False) -> int:
    """Open the dashboard in a native window. Returns a process exit code."""
    if not _claim_singleton():
        # Same trap as the failure path below: a console=False build has no
        # stderr, so a second double-click did nothing whatsoever and looked
        # like the app was broken.
        print("Builders Gate is already running.", file=sys.stderr)
        _notify("Builders Gate is already running",
                "Another copy is already open. Check your taskbar, or your "
                "browser if it fell back to running there.")
        return 0

    try:
        import webview  # pywebview
    except ImportError:
        print(
            "bgate app needs pywebview, which is not installed.\n"
            "\n"
            '    pip install "builders-gate[desktop]"\n'
            "\n"
            "Or keep using the browser dashboard, which needs nothing extra:\n"
            "\n"
            "    bgate serve\n",
            file=sys.stderr,
        )
        return 1

    import uvicorn

    from bgate_ui.app import app, _root_or_none

    port = port or _free_port()
    url = f"http://127.0.0.1:{port}"

    root = _root_or_none()
    print(f"builders gate · desktop window on {url}")
    if root is None:
        print("  no project here yet — the window will offer to create one")
    else:
        print(f"  project: {root}")

    # 127.0.0.1, same as `bgate serve`: a local window onto a local store.
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    # daemon=True is what lets closing the window end the process. uvicorn
    # installs signal handlers only on the main thread, so it gets none here and
    # would otherwise keep the interpreter alive after the GUI loop returns.
    thread = threading.Thread(target=server.run, name="bgate-uvicorn", daemon=True)
    thread.start()

    if not _wait_for_server(port):
        print(
            f"the dashboard server did not come up on {url} within 20s",
            file=sys.stderr,
        )
        return 1

    try:
        webview.create_window(
            WINDOW_TITLE,
            url,
            width=DEFAULT_SIZE[0],
            height=DEFAULT_SIZE[1],
            min_size=(max(MIN_SIZE[0], _MIN_USABLE_WIDTH), MIN_SIZE[1]),
            background_color="#0a0a0c",  # --bg, so first paint is not a white flash
            text_select=True,            # log lines and paths are meant to be copied
        )
        webview.start(debug=debug)
    except Exception as exc:                                   # noqa: BLE001
        # The window is a convenience, not the product. Losing it must not cost
        # the user the app.
        #
        # Two real causes seen so far. On Windows 10 the WebView2 runtime may
        # not be present (it ships with 11). And in the PyInstaller build,
        # pywebview reaches WebView2 through pythonnet, whose .NET hosting does
        # not reliably initialise inside a bundle — "Failed to resolve
        # Python.Runtime.Loader.Initialize". Fighting .NET hosting in a frozen
        # app is not a fight worth having when the fallback is this good: the
        # server is already up and the dashboard is a web app, so hand the user
        # their own browser and keep serving.
        #
        # This used to print to stderr and return 1. In a console=False build
        # there is no stderr anyone can see, so the app simply vanished on
        # double-click while the disk spun. That was the actual bug report.
        return _fallback_to_browser(url, exc, server, thread)

    # webview.start() returns when the last window closes. Ask uvicorn to stop
    # so an in-flight request gets to finish rather than dying with the process.
    server.should_exit = True
    thread.join(timeout=5.0)
    return 0


def _fallback_to_browser(url, exc, server, thread) -> int:
    """Open the default browser and keep serving until the user closes us."""
    import webbrowser

    print(f"could not open the desktop window: {exc}", file=sys.stderr)
    print(f"opening {url} in your browser instead", file=sys.stderr)

    opened = False
    try:
        opened = webbrowser.open(url)
    except Exception:                                          # noqa: BLE001
        pass

    # A windowed build has no console, so this dialog is the only place the user
    # will ever read this — and because it BLOCKS, it doubles as the thing
    # keeping the process alive. Lead with that; a dialog people dismiss on
    # reflex would take the server down with it.
    where = (f"Your browser has been opened at:\n{url}"
             if opened else
             f"Open this in your browser:\n{url}")
    _notify(
        "Builders Gate is running — keep this open",
        f"KEEP THIS MESSAGE OPEN while you use Builders Gate.\n"
        f"Closing it shuts the server down.\n\n"
        f"{where}\n\n"
        f"The desktop window could not open on this machine, so the dashboard "
        f"is running in your browser instead. Everything works the same.\n\n"
        f"Technical detail: {exc}"
    )

    # The dialog has been dismissed, so the user is finished. Shut the server
    # down rather than leaving an orphan holding a port.
    server.should_exit = True
    thread.join(timeout=5.0)
    return 0


def _notify(title: str, message: str) -> None:
    """A message box, or stderr if even that is unavailable."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x40)  # MB_ICONINFORMATION
        return
    except Exception:                                          # noqa: BLE001
        pass
    print(f"{title}: {message}", file=sys.stderr)
