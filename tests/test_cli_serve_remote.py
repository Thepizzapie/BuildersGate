"""bgate serve --remote passes remote=True through to bgate_ui.app.serve.

NOTE on entry point: the task brief's sketch assumed `bgate_cli.main.main`
takes an `argv` parameter. It does not — `main() -> int` reads directly from
`sys.argv[1:]` (see bgate_cli/main.py:1208-1211: `args = sys.argv[1:]`).
So this test monkeypatches `sys.argv` instead of passing argv to `main()`,
which is the real, unit-testable dispatcher `python -m bgate_cli` and the
`bgate` console script both route through.
"""
from __future__ import annotations

import sys


def test_serve_passes_remote_flag(monkeypatch):
    calls = {}
    import bgate_ui.app as appmod
    monkeypatch.setattr(appmod, "serve", lambda **k: calls.update(k))

    import bgate_cli.main as cli
    monkeypatch.setattr(sys, "argv", ["bgate", "serve", "--remote", "--port", "7799"])

    assert cli.main() == 0
    assert calls == {"port": 7799, "remote": True}


def test_serve_defaults_remote_false(monkeypatch):
    calls = {}
    import bgate_ui.app as appmod
    monkeypatch.setattr(appmod, "serve", lambda **k: calls.update(k))

    import bgate_cli.main as cli
    monkeypatch.setattr(sys, "argv", ["bgate", "serve"])

    assert cli.main() == 0
    assert calls == {"port": 7788, "remote": False}


def test_serve_port_still_works_without_remote(monkeypatch):
    calls = {}
    import bgate_ui.app as appmod
    monkeypatch.setattr(appmod, "serve", lambda **k: calls.update(k))

    import bgate_cli.main as cli
    monkeypatch.setattr(sys, "argv", ["bgate", "serve", "--port", "9000"])

    assert cli.main() == 0
    assert calls == {"port": 9000, "remote": False}


def test_app_passes_remote_flag(monkeypatch):
    calls = {}
    from bgate_ui.window import desktop
    monkeypatch.setattr(desktop, "run", lambda **k: calls.update(k) or 0)

    import bgate_cli.main as cli
    monkeypatch.setattr(sys, "argv", ["bgate", "app", "--remote"])

    assert cli.main() == 0
    assert calls == {"port": None, "debug": False, "remote": True}
