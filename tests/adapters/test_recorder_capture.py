"""What ffmpeg gets pointed at, and why it is never `title=`.

THE BUG THIS FILE EXISTS FOR: every playtest recorded a black rectangle with a
live mouse cursor moving over it. `gdigrab -i title=X` asks GDI for that
window's device context, and a Godot game never draws into one — it renders
through Vulkan/D3D and presents a swapchain the compositor owns, so GDI returns
the window's cleared background. gdigrab then composites the system cursor on
top itself, which is why the cursor was the only thing that ever moved.
Measured against the Godot editor at the time of the fix: every pixel of a
`title=` grab was RGB(36,36,36), while a desktop grab of the same screen came
back with a full 0-255 range.

These are argument-shape assertions, not capture tests — a real grab needs a
real window and a real ffmpeg, and lives behind the `slow` marker elsewhere.
The shape is the part that regressed, and it is the part a reader will be
tempted to "simplify" back to `title=`.
"""
from __future__ import annotations

import sys

import pytest

from bgate_adapters import recorder


def _args(title, *, fps=30):
    return recorder._video_input(title, fps)[0]


class TestItNeverPointsGdigrabAtAWindow:
    def test_a_named_window_is_still_a_desktop_grab(self, monkeypatch):
        monkeypatch.setattr(recorder, "window_rect",
                            lambda _t: {"x": 10, "y": 20,
                                        "width": 640, "height": 480})
        args = _args("Downsizing")
        assert "-i" in args and args[args.index("-i") + 1] == "desktop"
        assert not any(a.startswith("title=") for a in args), (
            "back to `title=` — that is the black-recording bug returning")

    def test_no_window_is_a_desktop_grab_cropped_to_one_screen(self, monkeypatch):
        """The fallback is ONE monitor, never every monitor at once.

        This used to assert the opposite - that an untargeted grab took the
        whole canvas - and the whole canvas is the virtual desktop. Measured on
        the machine that reported it: 5760x1080 across three screens, so every
        recording was an editor, a browser and a wallpaper with the game a third
        of the way across. "I do not like how playtest records all screens."
        """
        monkeypatch.setattr(recorder, "primary_rect",
                            lambda: {"x": 0, "y": 0, "width": 1920, "height": 1080})
        args = _args(None)
        assert args[args.index("-i") + 1] == "desktop"
        assert args[args.index("-video_size") + 1] == "1920x1080"
        assert args[args.index("-offset_x") + 1] == "0"


class TestTheCrop:
    def test_it_carries_the_window_rect(self, monkeypatch):
        monkeypatch.setattr(recorder, "window_rect",
                            lambda _t: {"x": 10, "y": 20,
                                        "width": 640, "height": 480})
        args = _args("Downsizing")
        assert args[args.index("-offset_x") + 1] == "10"
        assert args[args.index("-offset_y") + 1] == "20"
        assert args[args.index("-video_size") + 1] == "640x480"

    def test_the_offsets_are_absolute_not_origin_relative(self, monkeypatch):
        """gdigrab assigns clip_rect.left = offset_x outright once -video_size is
        set. Subtracting the virtual-desktop origin first put the crop a monitor
        to the right and ffmpeg refused the input."""
        monkeypatch.setattr(recorder, "window_rect",
                            lambda _t: {"x": -1920, "y": 0,
                                        "width": 800, "height": 600})
        args = _args("Downsizing")
        assert args[args.index("-offset_x") + 1] == "-1920"

    def test_an_unlocatable_window_falls_back_to_one_screen_not_all_of_them(
            self, monkeypatch):
        """Still records rather than dropping the session - but bounded.

        A black recording is useless and a three-monitor one is embarrassing;
        one screen is neither.
        """
        monkeypatch.setattr(recorder, "window_rect", lambda _t: None)
        monkeypatch.setattr(recorder, "primary_rect",
                            lambda: {"x": 0, "y": 0, "width": 1920, "height": 1080})
        args, note = recorder._video_input("Downsizing", 30)
        assert args[args.index("-i") + 1] == "desktop"
        assert args[args.index("-video_size") + 1] == "1920x1080"
        assert "could not be located" in note

    def test_with_no_monitor_metrics_it_still_records(self, monkeypatch):
        """Off Windows, or if the metrics call fails, an uncropped grab beats no
        recording at all - and the note says which one happened."""
        monkeypatch.setattr(recorder, "window_rect", lambda _t: None)
        monkeypatch.setattr(recorder, "primary_rect", lambda: None)
        args, note = recorder._video_input("Downsizing", 30)
        assert args[args.index("-i") + 1] == "desktop"
        assert "-video_size" not in args
        assert "whole desktop" in note

    def test_the_note_says_what_is_really_being_captured(self, monkeypatch):
        monkeypatch.setattr(recorder, "window_rect",
                            lambda _t: {"x": 0, "y": 0,
                                        "width": 1920, "height": 1032})
        _, note = recorder._video_input("Downsizing", 30)
        assert "1920x1032" in note and "Downsizing" in note


class TestTheCursorIsStillDrawn:
    def test_draw_mouse_is_explicit(self):
        """It was only ever on by default. Now that the cursor is no longer the
        one thing that survives, keep it deliberate rather than inherited."""
        assert _args(None)[_args(None).index("-draw_mouse") + 1] == "1"


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 geometry")
class TestWindowRect:
    def test_a_window_that_does_not_exist_is_none(self):
        assert recorder.window_rect("no such window exists anywhere ever") is None

    def test_a_real_window_is_even_sized_and_inside_the_desktop(self):
        """An odd-width crop is rejected when the input is opened, before any
        scale filter gets a chance to fix it — and a rect that leaves the canvas
        is the "Capture area extends outside window area" refusal.

        Run against whatever window this machine happens to have open, because
        the geometry being checked is Win32's, not ours. Skipped on a machine
        with no windows at all (a headless CI runner).
        """
        import ctypes

        windows = [w for w in recorder.list_windows() if w.get("title")]
        rect = next((r for r in (recorder.window_rect(w["title"])
                                 for w in windows) if r), None)
        if rect is None:
            pytest.skip("no locatable top-level window on this machine")

        user32 = ctypes.windll.user32
        vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        vr = vx + user32.GetSystemMetrics(78)
        vb = vy + user32.GetSystemMetrics(79)

        assert rect["width"] % 2 == 0 and rect["height"] % 2 == 0
        assert rect["width"] > 0 and rect["height"] > 0
        assert vx <= rect["x"] and rect["x"] + rect["width"] <= vr
        assert vy <= rect["y"] and rect["y"] + rect["height"] <= vb
