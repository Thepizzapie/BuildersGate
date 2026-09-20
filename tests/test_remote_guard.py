import os
import sys
import types
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Minimal project so ensure_token has a home.
    (tmp_path / ".bgate").mkdir()
    monkeypatch.setenv("BGATE_ROOT", str(tmp_path))
    # tests/conftest.py's autouse `_no_dashboard_auth` sets BGATE_NO_AUTH=1 for
    # the whole suite so ~350 unrelated tests don't have to plumb a token. This
    # module exercises the guard itself, so re-enable it here -- same fix
    # tests/test_auth_guard.py's `guarded` fixture already applies.
    monkeypatch.delenv("BGATE_NO_AUTH", raising=False)
    from bgate_ui import app as appmod, api as apimod
    token = apimod.ensure_token(tmp_path)
    c = TestClient(appmod.app)
    return c, token


def test_tailnet_host_rejected_when_remote_off(client, monkeypatch):
    monkeypatch.delenv("BGATE_REMOTE_HOSTS", raising=False)
    c, token = client
    r = c.post("/api/gate", json={"mode": "open"},
               headers={"host": "100.64.0.9:7788", "x-bgate-token": token})
    assert r.status_code == 403  # host gate: not loopback


def test_tailnet_host_allowed_with_phone_token_when_remote_on(client, monkeypatch, tmp_path):
    from bgate_ui import remote
    remote._reset_for_tests()
    monkeypatch.setenv("BGATE_REMOTE_HOSTS", "100.64.0.9")
    c, _ = client
    r = c.post("/api/gate", json={"mode": "open"},
               headers={"host": "100.64.0.9:7788",
                        "x-bgate-token": remote.ensure_token(tmp_path)})
    assert r.status_code != 403  # host admitted; 200 or a handler-level code


def test_the_dashboard_token_does_not_open_the_tailnet_door(client, monkeypatch):
    """The phone has its own credential. ui-token is what every fetch from
    the desktop page carries, and it must be useless from the tailnet side,
    or rotating the phone's token would not actually cut a phone off."""
    from bgate_ui import remote
    remote._reset_for_tests()
    monkeypatch.setenv("BGATE_REMOTE_HOSTS", "100.64.0.9")
    c, token = client
    r = c.get("/api/state", headers={"host": "100.64.0.9:7788", "x-bgate-token": token})
    assert r.status_code == 401


def test_tailnet_host_without_token_401(client, monkeypatch):
    monkeypatch.setenv("BGATE_REMOTE_HOSTS", "100.64.0.9")
    c, _ = client
    r = c.post("/api/gate", json={"mode": "open"},
               headers={"host": "100.64.0.9:7788"})
    assert r.status_code == 401


def test_evil_host_still_403_when_remote_on(client, monkeypatch):
    monkeypatch.setenv("BGATE_REMOTE_HOSTS", "100.64.0.9")
    c, token = client
    r = c.post("/api/gate", json={"mode": "open"},
               headers={"host": "evil.com:7788", "x-bgate-token": token})
    assert r.status_code == 403


def test_remote_bind_sets_env_and_returns_ip(monkeypatch):
    from bgate_ui import app as appmod, tailnet
    got = appmod._remote_bind(7788, detect=lambda **k: tailnet.TailnetAddr(ip="100.64.0.9"))
    assert got == ("100.64.0.9", ["100.64.0.9"])


def test_remote_bind_none_when_no_tailnet():
    from bgate_ui import app as appmod
    assert appmod._remote_bind(7788, detect=lambda **k: None) is None


@pytest.fixture
def _serve_env(tmp_path, monkeypatch):
    """Isolate serve()'s startup chatter from the real dev project, and stub
    the two helpers that are irrelevant to the remote-mode bind decision:
    _serving_elsewhere (a real loopback probe) and _print_pairing (token +
    QR printing, which needs nothing asserted here)."""
    monkeypatch.delenv("BGATE_NO_AUTH", raising=False)   # remote mode refuses to start under it
    (tmp_path / ".bgate").mkdir()
    monkeypatch.setenv("BGATE_ROOT", str(tmp_path))
    from bgate_ui import app as appmod
    monkeypatch.setattr(appmod, "_serving_elsewhere", lambda port, root: "")
    monkeypatch.setattr(appmod, "_print_pairing", lambda *a, **k: None)
    return appmod


def test_serve_remote_binds_to_tailnet_ip_and_sets_env(_serve_env, monkeypatch):
    """serve(remote=True) binds uvicorn to every interface (a bind to the
    single tailnet IP dropped a phone's packets), never 127.0.0.1 only, and
    publishes the detected tailnet IP via BGATE_REMOTE_HOSTS so the guard
    (see the tailnet_host_* tests above) admits it and nothing else."""
    appmod = _serve_env
    import uvicorn

    monkeypatch.delenv("BGATE_REMOTE_HOSTS", raising=False)
    monkeypatch.setattr(appmod, "_remote_bind",
                        lambda port: ("100.64.0.9", ["100.64.0.9"]))

    calls = {}

    def fake_run(app, host=None, port=None, log_level=None):
        calls["host"] = host
        calls["port"] = port

    monkeypatch.setattr(uvicorn, "run", fake_run)

    appmod.serve(port=7788, remote=True)

    assert calls.get("host") == "0.0.0.0"
    assert os.environ["BGATE_REMOTE_HOSTS"] == "100.64.0.9"


def test_serve_remote_refuses_before_bind_when_no_tailnet(_serve_env, monkeypatch):
    """No tailnet address -> serve() must refuse BEFORE calling uvicorn.run,
    not fall back to a loopback (or worse, a wildcard) bind."""
    appmod = _serve_env
    import uvicorn

    monkeypatch.setattr(appmod, "_remote_bind", lambda port: None)

    called = {"run": False}

    def fake_run(*a, **k):
        called["run"] = True

    monkeypatch.setattr(uvicorn, "run", fake_run)

    with pytest.raises(SystemExit) as exc_info:
        appmod.serve(port=7788, remote=True)

    assert exc_info.value.code == 2
    assert called["run"] is False


# ── /pair: the QR page the desktop app opens (it has no terminal) ─────────

def test_pair_page_is_404_when_remote_off(client, monkeypatch):
    c, _ = client
    monkeypatch.delenv("BGATE_REMOTE_HOSTS", raising=False)
    assert c.get("/pair").status_code == 404


def test_pair_page_serves_the_phone_token_and_qr_on_loopback(client, monkeypatch, tmp_path):
    from bgate_ui import remote
    c, token = client
    monkeypatch.setenv("BGATE_REMOTE_HOSTS", "100.64.0.9")
    r = c.get("/pair", headers={"host": "127.0.0.1:7788"})
    assert r.status_code == 200
    assert remote.ensure_token(tmp_path) in r.text
    assert token not in r.text          # the dashboard's own token never leaves the page
    assert "http://100.64.0.9:7788" in r.text
    pytest.importorskip("segno")
    assert 'src="data:image/svg+xml' in r.text


def test_pair_page_refused_from_the_tailnet_side(client, monkeypatch, tmp_path):
    """The phone has no business fetching the page that hands out the
    credential the phone is supposed to scan."""
    c, token = client
    monkeypatch.setenv("BGATE_REMOTE_HOSTS", "100.64.0.9")
    from bgate_ui import remote
    remote._reset_for_tests()
    r = c.get("/pair", headers={"host": "100.64.0.9:7788"})
    assert r.status_code == 404            # the page is not on the tailnet side's map at all
    assert token not in r.text
    # and even WITH the phone token, the page is loopback-only
    r = c.get("/pair", headers={"host": "100.64.0.9:7788",
                                "x-bgate-token": remote.ensure_token(tmp_path)})
    assert r.status_code == 404
    assert token not in r.text


# ── bgate app --remote: the window binds loopback AND the tailnet IP ──────

@pytest.fixture
def _desktop_env(tmp_path, monkeypatch):
    monkeypatch.delenv("BGATE_NO_AUTH", raising=False)   # remote mode refuses to start under it
    (tmp_path / ".bgate").mkdir()
    monkeypatch.setenv("BGATE_ROOT", str(tmp_path))
    monkeypatch.delenv("BGATE_REMOTE_HOSTS", raising=False)
    from bgate_ui.window import desktop
    monkeypatch.setattr(desktop, "_claim_singleton", lambda: True)
    monkeypatch.setattr(desktop, "_wait_for_server", lambda port, timeout=20.0: True)
    monkeypatch.setattr(desktop, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(desktop, "_free_port", lambda: 7790)
    # Stop before any window opens: the native path is the first thing after
    # the server is up, and a failing import lands in the pywebview fallback.
    monkeypatch.setattr(desktop, "_run_native", lambda *a, **k: 0)
    # The native window is Win32 (ctypes.WinDLL at import - an AttributeError,
    # not an ImportError, so importorskip does not catch it); elsewhere these
    # three tests are not about anything that can run.
    if sys.platform != "win32":
        pytest.skip("the native window is Win32 only")
    # run() imports pywebview only to refuse without it; CI has no desktop
    # extra, and nothing after the import is exercised here.
    monkeypatch.setitem(sys.modules, "webview", types.ModuleType("webview"))
    from bgate_ui.window import webview2
    monkeypatch.setattr(webview2, "available", lambda: (True, ""))
    return desktop


def test_app_remote_listens_on_both_addresses(_desktop_env, monkeypatch):
    desktop = _desktop_env
    import uvicorn
    from bgate_ui import app as appmod
    monkeypatch.setattr(appmod, "_remote_bind",
                        lambda port: ("100.64.0.9", ["100.64.0.9"]))
    bound = []
    monkeypatch.setattr(desktop, "_listen",
                        lambda host, port: bound.append((host, port)) or object())
    seen = {}
    monkeypatch.setattr(uvicorn.Server, "run",
                        lambda self, sockets=None: seen.update(sockets=sockets))
    opened = []
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))

    assert desktop.run(port=7790, remote=True) == 0
    import time
    time.sleep(0.05)  # the server "runs" on its daemon thread

    assert bound == [("127.0.0.1", 7790), ("100.64.0.9", 7790)]
    assert len(seen["sockets"]) == 2
    assert os.environ["BGATE_REMOTE_HOSTS"] == "100.64.0.9"
    assert opened == ["http://127.0.0.1:7790/pair"]


def test_app_without_remote_is_loopback_only(_desktop_env, monkeypatch):
    desktop = _desktop_env
    import uvicorn
    seen = {}
    monkeypatch.setattr(uvicorn.Server, "run",
                        lambda self, sockets=None: seen.update(sockets=sockets))
    monkeypatch.setattr(desktop, "_listen",
                        lambda *a: pytest.fail("no socket should be pre-bound"))
    assert desktop.run(port=7790) == 0
    import time
    time.sleep(0.05)
    assert seen["sockets"] is None
    assert "BGATE_REMOTE_HOSTS" not in os.environ


def test_app_remote_refuses_without_tailnet(_desktop_env, monkeypatch):
    desktop = _desktop_env
    import uvicorn
    from bgate_ui import app as appmod
    monkeypatch.setattr(appmod, "_remote_bind", lambda port: None)
    monkeypatch.setattr(uvicorn.Server, "run",
                        lambda self, sockets=None: pytest.fail("must not start"))
    assert desktop.run(port=7790, remote=True) == 2
