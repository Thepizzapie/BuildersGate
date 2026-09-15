import os
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


def test_tailnet_host_allowed_with_token_when_remote_on(client, monkeypatch):
    monkeypatch.setenv("BGATE_REMOTE_HOSTS", "100.64.0.9")
    c, token = client
    r = c.post("/api/gate", json={"mode": "open"},
               headers={"host": "100.64.0.9:7788", "x-bgate-token": token})
    assert r.status_code != 403  # host admitted; 200 or a handler-level code


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
    (tmp_path / ".bgate").mkdir()
    monkeypatch.setenv("BGATE_ROOT", str(tmp_path))
    from bgate_ui import app as appmod
    monkeypatch.setattr(appmod, "_serving_elsewhere", lambda port, root: "")
    monkeypatch.setattr(appmod, "_print_pairing", lambda *a, **k: None)
    return appmod


def test_serve_remote_binds_to_tailnet_ip_and_sets_env(_serve_env, monkeypatch):
    """serve(remote=True) must bind uvicorn to the DETECTED TAILNET IP, never
    127.0.0.1, and must publish it via BGATE_REMOTE_HOSTS so the guard (see
    the tailnet_host_* tests above) admits it."""
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

    assert calls.get("host") == "100.64.0.9"
    assert calls.get("host") != "127.0.0.1"
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
