"""Window controls for the app's own title bar.

The desktop window is frameless — it has no system caption, because the page
draws its own header instead (frontend/src/shell/TitleBar.tsx). The three
buttons at the right of that header need to reach the actual HWND, and this is
the channel.

WHY HTTP AND NOT A WEBVIEW2 MESSAGE CHANNEL. The obvious mechanism is
``window.chrome.webview.postMessage`` with an ``add_WebMessageReceived``
handler on the host side. bgate_ui/webview2.py talks to WebView2 through raw
COM vtable indices counted by hand, so that would mean another handler class,
another interface's IID, and another index pinned by hand against an interface
whose layout is not visible from Python — all to move the word "minimize". The
page is already holding a conversation with this exact process over loopback.
Three routes cost nothing and are testable from a terminal.

WHY THE HANDLE IS A MODULE GLOBAL. There is exactly one window per process and
it does not exist for most of the process's life: `bgate serve` has no window at
all, and neither does the browser fallback. So the state being modelled really
is "the one window, if there is one", and `attach()` is called by
bgate_ui.desktop when it opens and again with None when it closes. Every route
below answers `available: false` rather than failing when there is none, because
the same page is served to a browser tab where these buttons are not drawn.

NOTHING HERE IS GATED BY require_human, AND THAT IS A DECISION RATHER THAN AN
OVERSIGHT. These do not touch the project: they minimise, maximise, move and
close a window on the machine the server is already running on. A confirmation
dialog to close a window is the kind of safety that teaches people to click
through dialogs, and the drag route is called on every mousedown of the title
bar — a gate there would make the window undraggable.

What already stands between these and the outside world, from bgate_ui.api's
middleware, applied to every non-safe method:

  * the request Host must be loopback, or 403;
  * Sec-Fetch-Site must be same-origin or none, or 403;
  * an Origin header, if present, must match this dashboard, or 403;
  * a valid per-project x-bgate-token must be presented, or 401.

So the reachable worst case is a page inside this dashboard closing its own
window. Weighed against a title bar that cannot drag, that is the right trade —
but it IS a trade, and a future route added to this module that touches
anything beyond window state does not inherit the reasoning.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter

router = APIRouter()

# The live native window, or None. See the module docstring.
_window = None


def attach(win) -> None:
    """Register (or clear) the process's native window."""
    global _window
    _window = win


def current():
    return _window


def _state(extra: Optional[dict] = None) -> dict:
    win = _window
    if win is None:
        return {"available": False, "maximized": False, **(extra or {})}
    try:
        maximized = bool(win.is_maximized())
    except Exception:                                           # noqa: BLE001
        maximized = False
    return {"available": True, "maximized": maximized, **(extra or {})}


@router.get("/api/window/state")
def window_state() -> dict:
    """Whether this page is inside the app's own frameless window.

    THE PAGE DRAWS ITS TITLE BAR ONLY IF THIS SAYS SO. The same bundle is
    served to a browser tab, where a fake title bar with a close button that
    closes nothing would be worse than no title bar at all — and to the window
    running with BGATE_NATIVE_FRAME=1, which has a real system caption and must
    not get a second one drawn underneath it.
    """
    return _state()


@router.post("/api/window/minimize")
def window_minimize() -> dict:
    if _window is None:
        return _state({"ok": False, "why": "no native window"})
    _window.minimize()
    return _state({"ok": True})


@router.post("/api/window/maximize")
def window_maximize() -> dict:
    """Toggle: maximise when restored, restore when maximised.

    One route rather than two because the button is one button, and asking the
    page to decide which to call means the page holds a copy of the window
    state that can be wrong — the user can maximise with a double-click on the
    drag strip or a Win+Up at any time.
    """
    if _window is None:
        return _state({"ok": False, "why": "no native window"})
    _window.toggle_maximize()
    return _state({"ok": True})


@router.post("/api/window/drag")
def window_drag() -> dict:
    """Start moving the window, from a mousedown on the page's title bar.

    THE PAGE HAS TO ASK, because it cannot be dragged any other way. WebView2
    covers the whole client area with a child window, and a child window takes
    the mouse before the parent's WM_NCHITTEST is consulted — so claiming the
    top strip as HTCAPTION, which is what makes a frameless window draggable,
    has no effect on any pixel the page is drawing. Reporting the mousedown here
    hands the gesture to Windows, which runs its own modal move loop from there:
    edge snap, monitor changes and release are all its business, not ours, and
    exactly one message is sent per drag rather than one per mouse-move.
    """
    if _window is None:
        return _state({"ok": False, "why": "no native window"})
    _window.start_drag()
    return _state({"ok": True})


@router.post("/api/window/close")
def window_close() -> dict:
    if _window is None:
        return _state({"ok": False, "why": "no native window"})
    _window.close()
    return {"available": True, "ok": True, "closing": True}
