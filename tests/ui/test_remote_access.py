"""Settings > Phone: the door can be closed and reopened without a restart,
the token can be rotated, devices are seen and can be revoked one at a time,
and none of it is reachable from the phone's side."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bgate_ui import api as apimod, remote

PHONE = {"host": "100.64.0.9:7788", "user-agent": "BGATE-ios/1.0 (iPhone)"}
DESK = {"host": "127.0.0.1:7788"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / ".bgate").mkdir()
    monkeypatch.setenv("BGATE_ROOT", str(tmp_path))
    monkeypatch.delenv("BGATE_NO_AUTH", raising=False)
    monkeypatch.setenv("BGATE_REMOTE_HOSTS", "100.64.0.9")
    remote._reset_for_tests()
    from bgate_ui import app as appmod
    c = TestClient(appmod.app)
    desk = {**DESK, "x-bgate-token": apimod.ensure_token(tmp_path)}
    phone = {**PHONE, "x-bgate-token": remote.ensure_token(tmp_path)}
    return c, desk, phone


def test_a_phone_get_needs_the_phone_token(client):
    """GET was open on the loopback side (the page has to load before it can
    present a token). From the tailnet it is the whole project."""
    c, _, phone = client
    assert c.get("/api/state?lean=1", headers=PHONE).status_code == 401
    assert c.get("/api/state?lean=1", headers=phone).status_code == 200


def test_the_switch_closes_and_reopens_the_door_without_a_restart(client):
    c, desk, phone = client
    assert c.get("/api/queue", headers=phone).status_code == 200
    r = c.post("/api/remote/disable", headers=desk)
    assert r.status_code == 200 and r.json()["enabled"] is False
    r = c.get("/api/queue", headers=phone)
    assert r.status_code == 403 and r.json()["error"]["code"] == "remote_off"
    assert c.get("/api/queue", headers=desk).status_code == 200   # the desk is untouched
    assert c.post("/api/remote/enable", headers=desk).json()["enabled"] is True
    assert c.get("/api/queue", headers=phone).status_code == 200


def test_enable_refuses_when_the_server_has_no_tailnet_socket(client, monkeypatch):
    c, desk, _ = client
    monkeypatch.delenv("BGATE_REMOTE_HOSTS")
    r = c.post("/api/remote/enable", headers=desk)
    assert r.status_code == 400 and r.json()["error"]["detail"]["code"] == "not_listening"
    assert c.get("/api/remote", headers=desk).json()["restart_hint"]


def test_rotating_the_token_cuts_every_phone_off_and_keeps_the_desk(client, tmp_path):
    c, desk, phone = client
    old = phone["x-bgate-token"]
    assert c.get("/api/queue", headers=phone).status_code == 200
    r = c.post("/api/remote/rotate", headers=desk)
    assert r.status_code == 200
    new = r.json()["token"]
    assert new and new != old
    assert r.json()["devices"] == []          # the table belonged to the old token
    assert c.get("/api/queue", headers=phone).status_code == 401
    assert c.get("/api/queue", headers={**PHONE, "x-bgate-token": new}).status_code == 200
    assert c.get("/api/queue", headers=desk).status_code == 200
    assert apimod.ensure_token(tmp_path) == desk["x-bgate-token"]


def test_devices_are_seen_counted_and_can_be_revoked_one_at_a_time(client):
    c, desk, phone = client
    other = {**phone, "user-agent": "BGATE-ios/1.0 (iPad)"}
    c.get("/api/queue", headers=phone)
    c.get("/api/state?lean=1", headers=phone)
    c.get("/api/queue", headers=other)
    st = c.get("/api/remote", headers=desk).json()
    rows = {d["user_agent"]: d for d in st["devices"]}
    assert set(rows) == {PHONE["user-agent"], other["user-agent"]}
    assert rows[PHONE["user-agent"]]["requests"] == 2
    assert rows[PHONE["user-agent"]]["last_path"] == "/api/state"
    assert all(d["online"] for d in st["devices"])

    victim = rows[PHONE["user-agent"]]["id"]
    r = c.post(f"/api/remote/devices/{victim}/revoke", headers=desk)
    assert r.status_code == 200
    r = c.get("/api/queue", headers=phone)
    assert r.status_code == 403 and r.json()["error"]["code"] == "device_revoked"
    assert c.get("/api/queue", headers=other).status_code == 200
    st = c.get("/api/remote", headers=desk).json()
    me = next(d for d in st["devices"] if d["id"] == victim)
    assert me["revoked"] is True and me["refused"] == 1 and me["online"] is False
    assert st["refusals"][-1]["why"] == "device revoked"

    assert c.post(f"/api/remote/devices/{victim}/restore", headers=desk).status_code == 200
    assert c.get("/api/queue", headers=phone).status_code == 200


def test_forgetting_is_not_revoking(client):
    c, desk, phone = client
    c.get("/api/queue", headers=phone)
    dev = c.get("/api/remote", headers=desk).json()["devices"][0]["id"]
    assert c.delete(f"/api/remote/devices/{dev}", headers=desk).json()["devices"] == []
    assert c.get("/api/queue", headers=phone).status_code == 200
    assert len(c.get("/api/remote", headers=desk).json()["devices"]) == 1
    assert c.delete("/api/remote/devices", headers=desk).json()["devices"] == []
    assert c.post("/api/remote/devices/nope/revoke", headers=desk).status_code == 404


def test_a_wrong_token_shows_up_as_a_refusal_not_silence(client):
    c, desk, _ = client
    c.get("/api/queue", headers={**PHONE, "x-bgate-token": "stale"})
    st = c.get("/api/remote", headers=desk).json()
    assert st["devices"] == []
    assert st["refusals"][0]["why"] == "wrong or missing phone token"
    assert st["refusals"][0]["ip"]


def test_the_controls_do_not_exist_from_the_phone_side(client):
    """The phone must not be able to read the QR it is supposed to scan,
    reopen its own door, or un-revoke itself."""
    c, _, phone = client
    assert c.get("/api/remote", headers=phone).status_code == 404
    assert c.post("/api/remote/enable", headers=phone).status_code == 404
    assert c.post("/api/remote/rotate", headers=phone).status_code == 404
    assert c.post("/api/remote/devices/x/restore", headers=phone).status_code == 404


def test_status_carries_the_qr_and_the_url(client):
    c, desk, phone = client
    st = c.get("/api/remote", headers=desk).json()
    assert st["listening"] and st["enabled"]
    assert st["url"] == "http://100.64.0.9:7788"
    assert st["token"] == phone["x-bgate-token"]
    pytest.importorskip("segno")
    assert st["qr"].startswith("data:image/svg+xml")


def test_a_proxied_phone_is_named_by_its_forwarded_address(client):
    """Behind `tailscale serve` every phone arrives from 127.0.0.1; the
    proxy's X-Forwarded-For is the phone. Not trusted from a non-loopback
    peer, which would let a phone name itself."""
    c, desk, phone = client
    c.get("/api/queue", headers={**phone, "x-forwarded-for": "100.64.0.42"})
    st = c.get("/api/remote", headers=desk).json()
    assert st["devices"][0]["ip"] == "100.64.0.42"


def test_switching_the_project_does_not_log_the_phone_out(client, tmp_path, monkeypatch):
    """The phone pairs with the MACHINE. A per-project token meant that the
    moment the desk (or the phone itself) switched projects, every poll was
    401 and the phone said re-pair."""
    from bgate_core.store import project as _project
    c, _, phone = client
    other = tmp_path.parent / (tmp_path.name + "-other")
    _project.init(other, "Other Game", engine="godot")
    assert c.get("/api/state?lean=1", headers=phone).status_code == 200
    r = c.post("/api/project/select", json={"root": str(other)}, headers=phone)
    assert r.status_code == 200, r.text
    r = c.get("/api/state?lean=1", headers=phone)
    assert r.status_code == 200
    assert r.json()["project"]["name"] == "Other Game"


def test_the_web_build_and_its_telemetry_accept_the_token_as_a_cookie(client, tmp_path):
    """The phone plays /play/ in a web view; the engine's own .wasm/.pck
    fetches carry no header. The cookie works there and nowhere else."""
    c, _, phone = client
    web = tmp_path / "export" / "web"
    web.mkdir(parents=True)
    (web / "index.html").write_text("<html>game</html>", encoding="utf-8")
    (web / "index.pck").write_bytes(b"pck")
    cookie = {"host": PHONE["host"], "user-agent": "Mozilla/5.0 (iPhone) AppleWebKit"}
    c.cookies.set(apimod.PHONE_COOKIE, phone["x-bgate-token"])
    assert c.get("/play/", headers=cookie).status_code == 200
    assert c.get("/play/index.pck", headers=cookie).status_code == 200
    # the cookie opens the build, not the project
    assert c.get("/api/state?lean=1", headers=cookie).status_code == 401
    assert c.get("/api/queue", headers=cookie).status_code == 401
    # and a wrong cookie opens nothing
    c.cookies.set(apimod.PHONE_COOKIE, "stale")
    assert c.get("/play/", headers=cookie).status_code == 401


# ── the door is decided by every signal, not the Host header alone ─────────

def _peer(appmod, host_ip):
    """A TestClient whose socket peer is a tailnet address."""
    return TestClient(appmod.app, client=(host_ip, 40000))


def test_a_network_peer_writing_a_loopback_host_is_still_the_tailnet_side(client, tmp_path):
    """The Host gate alone was forgeable: a client on the tailnet (or a raw
    TCP relay) writes `Host: 127.0.0.1` and used to be served the desk's
    page - whose HTML carries the desk's own token."""
    from bgate_ui import app as appmod
    c = _peer(appmod, "100.64.0.9")
    r = c.get("/", headers={"host": "127.0.0.1:7788"})
    assert r.status_code == 404
    assert apimod.ensure_token(tmp_path) not in r.text
    r = c.get("/api/state?lean=1", headers={"host": "127.0.0.1:7788"})
    assert r.status_code == 401
    r = c.get("/api/state?lean=1", headers={"host": "127.0.0.1:7788",
                                             "x-bgate-token": remote.ensure_token()})
    assert r.status_code == 200


def test_a_proxied_request_is_the_tailnet_side_whatever_the_socket_says(client):
    """tailscale serve in HTTP mode arrives from 127.0.0.1 with X-Forwarded-For."""
    c, _, phone = client
    r = c.get("/api/queue", headers={"host": "127.0.0.1:7788",
                                     "x-forwarded-for": "100.64.0.42"})
    assert r.status_code == 401
    r = c.get("/api/queue", headers={"host": "127.0.0.1:7788",
                                     "x-forwarded-for": "100.64.0.42",
                                     "x-bgate-token": phone["x-bgate-token"]})
    assert r.status_code == 200


def test_the_tailnet_side_never_gets_the_page_or_the_door_controls(client, tmp_path):
    """Even with a valid phone token: `/` carries the desk token in its HTML,
    and /api/remote hands out the phone token and its QR."""
    c, _, phone = client
    assert c.get("/", headers=phone).status_code == 404
    assert apimod.ensure_token(tmp_path) not in c.get("/", headers=phone).text
    assert c.get("/api/remote", headers=phone).status_code == 404
    assert c.get("/pair", headers=phone).status_code == 404
    assert c.get("/static/app.css", headers=phone).status_code == 404


def test_reading_the_door_status_needs_the_desk_token_even_as_a_get(client):
    """Its body is the credential. A loopback GET without the dashboard's
    token (any local process, or a page that got past the Host gate) must
    not be handed the phone token and the QR."""
    c, desk, _ = client
    assert c.get("/api/remote", headers=DESK).status_code == 401
    assert c.get("/api/remote", headers=desk).status_code == 200


def test_the_switch_survives_a_restart(client):
    c, desk, phone = client
    c.post("/api/remote/disable", headers=desk)
    remote._switched_off = None            # what a fresh process starts with
    assert remote.enabled() is False
    assert c.get("/api/queue", headers=phone).status_code == 403
    c.post("/api/remote/enable", headers=desk)
    remote._switched_off = None
    assert remote.enabled() is True


def test_the_device_table_is_bounded(client):
    c, desk, phone = client
    for i in range(remote.MAX_DEVICES + 10):
        c.get("/api/queue", headers={**phone, "user-agent": f"agent-{i}"})
    assert len(c.get("/api/remote", headers=desk).json()["devices"]) == remote.MAX_DEVICES
