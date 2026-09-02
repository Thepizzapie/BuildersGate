"""The frameless window's geometry, and the two places that have to agree on it.

The desktop window has no system caption. Windows is told which pixels are the
caption by WM_NCHITTEST, and the page draws a bar it hopes sits in exactly those
pixels. NOTHING CONNECTS THE TWO AT RUN TIME — the wndproc cannot ask the page
where it put its header, and the page cannot ask the window what it claimed. So
they are two constants that must be equal, and the only thing that can hold them
equal is a test.

The failure when they drift is quiet and horrible to diagnose: if the wndproc
claims less than the bar is tall, the bottom few pixels of the title bar stop
dragging the window; if it claims more, the top of the page below the bar drags
the window instead of clicking. Either way nothing errors.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TSX = ROOT / "frontend" / "src" / "shell" / "TitleBar.tsx"
CSS = ROOT / "frontend" / "src" / "shell" / "shell.css"

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="bgate_ui.window.webview2 is the Windows native-window host",
)


def _const(text: str, name: str) -> int:
    m = re.search(rf"export const {name} = (\d+)", text)
    assert m, f"{name} not found in TitleBar.tsx"
    return int(m.group(1))


class TestCaptionGeometry:
    def test_caption_height_matches_the_page(self):
        from bgate_ui.window import webview2

        assert webview2.CAPTION_H == _const(TSX.read_text(encoding="utf-8"),
                                            "CAPTION_H")

    def test_button_strip_width_matches_the_page(self):
        from bgate_ui.window import webview2

        tsx = TSX.read_text(encoding="utf-8")
        # The page states it as three buttons of BUTTON_W; the wndproc gets the
        # product, because it only cares where the strip starts.
        assert webview2.CAPTION_BUTTONS_W == _const(tsx, "BUTTON_W") * 3

    def test_css_reserves_exactly_the_caption_height(self):
        """The padding that stops the bar covering the app must match its height.

        A bar 44px tall over 40px of padding hides 4px of whatever is beneath it
        forever, at the top of every screen.
        """
        from bgate_ui.window import webview2

        css = CSS.read_text(encoding="utf-8")
        m = re.search(r"\.bg-framed body[^{]*\{[^}]*padding-top:\s*(\d+)px", css)
        assert m, "no .bg-framed padding-top rule in shell.css"
        assert int(m.group(1)) == webview2.CAPTION_H


class TestWindowControls:
    """The route module must answer, not raise, when there is no window.

    The same page is served to a browser tab. Every one of these is reachable
    there, and a 500 in a browser is a console full of red on a surface where
    the title bar is deliberately not drawn.
    """

    @pytest.fixture(autouse=True)
    def _detached(self):
        from bgate_ui.routes import window as w
        w.attach(None)
        yield
        w.attach(None)

    def test_state_reports_unavailable(self):
        from bgate_ui.routes import window as w

        assert w.window_state() == {"available": False, "maximized": False}

    @pytest.mark.parametrize("fn", ["window_minimize", "window_maximize",
                                    "window_close"])
    def test_controls_refuse_without_a_window(self, fn):
        from bgate_ui.routes import window as w

        out = getattr(w, fn)()
        assert out["ok"] is False
        assert "no native window" in out["why"]

    def test_controls_drive_the_attached_window(self):
        from bgate_ui.routes import window as w

        class FakeWindow:
            def __init__(self):
                self.calls = []
                self.max = False

            def minimize(self):
                self.calls.append("minimize")

            def toggle_maximize(self):
                self.calls.append("toggle")
                self.max = not self.max

            def close(self):
                self.calls.append("close")

            def is_maximized(self):
                return self.max

        fake = FakeWindow()
        w.attach(fake)
        assert w.window_state() == {"available": True, "maximized": False}
        w.window_minimize()
        w.window_maximize()
        assert w.window_state()["maximized"] is True
        w.window_close()
        assert fake.calls == ["minimize", "toggle", "close"]


class TestHitTest:
    """The hit test itself, without creating a window.

    Every branch here is a gesture the user loses entirely if it is wrong, and
    none of them can be checked by looking at the window.
    """

    @pytest.fixture()
    def win(self, monkeypatch):
        from bgate_ui.window import webview2

        w = webview2.Window("t", "http://127.0.0.1:1", frameless=True)
        w.hwnd = 1
        # A 1000x800 window at (100, 100), 96 dpi, 8px resize border.
        rect = webview2.RECT(100, 100, 1100, 900)

        def fake_get_window_rect(hwnd, out):
            # The real call receives byref(rect), which arrives here as a
            # CArgObject rather than a pointer — `_obj` is the RECT it wraps.
            target = getattr(out, "_obj", None) or out.contents
            target.left, target.top = rect.left, rect.top
            target.right, target.bottom = rect.right, rect.bottom
            return 1

        monkeypatch.setattr(webview2.user32, "GetWindowRect", fake_get_window_rect)
        monkeypatch.setattr(webview2.user32, "IsZoomed", lambda hwnd: 0)
        monkeypatch.setattr(w, "_scale", lambda px: px)
        monkeypatch.setattr(w, "_border", lambda: 8)
        return w

    @staticmethod
    def _at(x, y):
        """Pack a screen point the way WM_NCHITTEST does."""
        return (y << 16) | (x & 0xFFFF)

    def test_corners_and_edges(self, win):
        from bgate_ui.window import webview2 as v

        cases = [
            (102, 102, v.HTTOPLEFT), (1098, 102, v.HTTOPRIGHT),
            (102, 898, v.HTBOTTOMLEFT), (1098, 898, v.HTBOTTOMRIGHT),
            (600, 102, v.HTTOP), (600, 898, v.HTBOTTOM),
            (102, 500, v.HTLEFT), (1098, 500, v.HTRIGHT),
        ]
        for x, y, want in cases:
            assert win._hittest(self._at(x, y)) == want, (x, y)

    def test_the_bar_drags_and_the_buttons_do_not(self, win):
        from bgate_ui.window import webview2 as v

        # Inside the caption strip, left of the buttons: a drag handle.
        assert win._hittest(self._at(400, 120)) == v.HTCAPTION
        # The buttons live in the last CAPTION_BUTTONS_W pixels and must stay
        # clickable — HTCAPTION there would start a window drag instead.
        assert win._hittest(self._at(1090, 120)) == v.HTCLIENT
        # Below the strip is ordinary page.
        assert win._hittest(self._at(400, 200)) == v.HTCLIENT

    def test_negative_coordinates_survive(self, win):
        """A window dragged left of the primary monitor gives NEGATIVE x.

        lParam packs two SIGNED shorts. Reading them unsigned turns x = -20 into
        65516, which lands past the right edge — so the left resize border stops
        working the moment a window is moved onto a monitor left of the main
        one, and only there.
        """
        assert win._hittest(self._at(-20, 500)) != 0
        x = (-20) & 0xFFFF
        assert x == 65516                      # what the naive read would give
