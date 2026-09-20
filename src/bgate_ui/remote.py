"""Phone access: the companion app's credential, its on/off switch, and the
table of devices using it.

Three facts that were tangled before this module existed:

  1. THE PHONE HAD THE DASHBOARD'S OWN TOKEN. The QR handed out ui-token, the
     same secret every fetch from the desktop page carries. Rotating it to cut
     a phone off would have logged the desktop out of itself. The phone now
     has its own token, so rotating it re-pairs the phones and nothing else.
     It is MACHINE-WIDE (`~/.bgate/remote-token`, BGATE_HOME honoured), not
     per project: a phone pairs with the desk, and switching the desk to
     another project - from the browser or from the phone itself - must not
     log the phone out. A per-project token did exactly that.
  2. THERE WAS NO SWITCH. Remote mode was decided at `serve --remote` and
     lasted until the process died. The bind still is (a socket cannot change
     its address at runtime), but admission is checked per request, so the
     tailnet side can be closed and reopened at will without a restart.
  3. NOBODY KNEW WHO WAS CONNECTED. Every tailnet-side request now lands in a
     device table (address + user agent, first/last seen, request count), and
     a device can be revoked by hand without touching the others.

Process-level state on purpose: a device table is a fact about THIS server
process's lifetime, and a revocation that outlived the process would be a
surprise the next time it started.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Optional

TOKEN_FILENAME = "remote-token"

#: A device is "online" when it has spoken inside this window. The companion
#: app polls every few seconds, so 20 s is one missed poll, not a verdict.
ONLINE_WINDOW_S = 20.0
#: Refusals are kept as a short ring so a wrong-token phone shows up as
#: "something is knocking" rather than as silence.
REFUSAL_RING = 8

#: The device table is bounded: a client cycling user agents must not be a
#: way to grow this process without limit. Oldest non-revoked rows go first.
MAX_DEVICES = 64

_lock = threading.Lock()
_switched_off: Optional[bool] = None      # None: not read from disk yet
_devices: dict[str, dict[str, Any]] = {}
_refusals: list[dict[str, Any]] = []
_rotated_at: float = 0.0


# ---------------------------------------------------------------------------
# The credential
# ---------------------------------------------------------------------------

def token_path(root: Optional[Path] = None) -> Path:
    """Machine-wide. `root` is accepted and ignored so every caller that
    holds a project can keep passing it."""
    from bgate_core.store.project import user_dir
    return user_dir() / TOKEN_FILENAME


def ensure_token(root: Optional[Path] = None) -> str:
    """Read (or mint) the phone token. 0600 in ~/.bgate, beside the keys."""
    path = token_path(root)
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    return _write_token(path)


def rotate_token(root: Optional[Path] = None) -> str:
    """Mint a new phone token. Every paired phone is cut off at once and has
    to scan the new QR; the device table is cleared with it, because every
    row in it belonged to the old credential."""
    global _rotated_at
    token = _write_token(token_path(root))
    with _lock:
        _devices.clear()
        _refusals.clear()
        _rotated_at = time.time()
    return token


def _write_token(path: Path) -> str:
    token = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------

def host() -> str:
    """The tailnet host the server was started on, or "" when the process
    was not started with --remote (in which case nothing here can help: the
    socket is loopback-only and only a restart changes that)."""
    raw = os.environ.get("BGATE_REMOTE_HOSTS", "")
    return next((h.strip() for h in raw.split(",") if h.strip()), "")


def listening() -> bool:
    return bool(host())


def _switch_path() -> Path:
    from bgate_core.store.project import user_dir
    return user_dir() / "remote-switch.json"


def _switched() -> bool:
    """The switch, read once from disk. A door closed from the panel stays
    closed across a restart: a restart that silently reopened it would be
    the one time the operator was not looking."""
    global _switched_off
    if _switched_off is None:
        try:
            import json
            _switched_off = bool(json.loads(
                _switch_path().read_text(encoding="utf-8")).get("off"))
        except (OSError, ValueError, AttributeError):
            _switched_off = False
    return _switched_off


def enabled() -> bool:
    """Admit tailnet-side requests right now? Requires the --remote bind AND
    the switch not having been turned off."""
    return listening() and not _switched()


def set_enabled(on: bool) -> bool:
    global _switched_off
    with _lock:
        _switched_off = not on
        try:
            import json
            p = _switch_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"off": not on, "at": int(time.time())}),
                         encoding="utf-8")
        except OSError:
            pass
    return enabled()


# ---------------------------------------------------------------------------
# The device table
# ---------------------------------------------------------------------------

def device_key(ip: str, user_agent: str) -> str:
    return hashlib.sha1(f"{ip}|{user_agent}".encode("utf-8")).hexdigest()[:12]


_LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def _describe(request) -> tuple[str, str, str]:
    client = getattr(request, "client", None)
    ip = (client.host if client else "") or ""
    # Behind `tailscale serve` every phone arrives from loopback; the proxy
    # names the real peer in X-Forwarded-For. Only trusted when the socket
    # peer IS loopback, so a tailnet client cannot spoof its own address.
    if ip in _LOOPBACK:
        fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if fwd:
            ip = fwd
    ua = (request.headers.get("user-agent") or "")[:160]
    path = request.url.path
    return ip, ua, path


def note_request(request) -> dict[str, Any]:
    """Record an admitted tailnet-side request against its device. Returns
    the row, whose `revoked` flag the guard reads."""
    ip, ua, path = _describe(request)
    key = device_key(ip, ua)
    now = time.time()
    with _lock:
        row = _devices.get(key)
        if row is None:
            if len(_devices) >= MAX_DEVICES:
                victims = sorted((r for r in _devices.values() if not r["revoked"]),
                                 key=lambda r: r["last_seen"])
                for r in victims[:len(_devices) - MAX_DEVICES + 1]:
                    _devices.pop(r["id"], None)
            row = _devices[key] = {
                "id": key, "ip": ip, "user_agent": ua, "first_seen": now,
                "last_seen": now, "requests": 0, "last_path": path,
                "revoked": False, "refused": 0}
        row["last_seen"] = now
        row["requests"] += 1
        row["last_path"] = path
        return dict(row)


def note_refusal(request, why: str) -> None:
    """A tailnet-side request that did not get in: wrong token, revoked
    device, switch off. Kept so the panel can say something is knocking."""
    ip, ua, path = _describe(request)
    key = device_key(ip, ua)
    now = time.time()
    with _lock:
        row = _devices.get(key)
        if row is not None:
            row["refused"] += 1
            row["last_seen"] = now
        _refusals.append({"device": key, "ip": ip, "user_agent": ua,
                          "path": path, "why": why, "at": now})
        del _refusals[:-REFUSAL_RING]


def is_revoked(request) -> bool:
    ip, ua, _ = _describe(request)
    with _lock:
        row = _devices.get(device_key(ip, ua))
        return bool(row and row["revoked"])


def devices() -> list[dict[str, Any]]:
    now = time.time()
    with _lock:
        rows = [dict(r) for r in _devices.values()]
    for r in rows:
        r["online"] = (not r["revoked"]) and (now - r["last_seen"]) <= ONLINE_WINDOW_S
        r["idle_s"] = round(now - r["last_seen"], 1)
    rows.sort(key=lambda r: r["last_seen"], reverse=True)
    return rows


def set_revoked(device_id: str, revoked: bool) -> Optional[dict[str, Any]]:
    with _lock:
        row = _devices.get(device_id)
        if row is None:
            return None
        row["revoked"] = revoked
        return dict(row)


def forget(device_id: Optional[str] = None) -> int:
    """Drop one device row, or all of them. Forgetting is not revoking: a
    forgotten device that still holds a valid token reappears on its next
    request. Rotate the token to actually cut it off."""
    with _lock:
        if device_id is None:
            n = len(_devices)
            _devices.clear()
            _refusals.clear()
            return n
        return 1 if _devices.pop(device_id, None) is not None else 0


def refusals() -> list[dict[str, Any]]:
    with _lock:
        return [dict(r) for r in _refusals]


def _reset_for_tests() -> None:
    global _switched_off, _rotated_at
    with _lock:
        _switched_off = False
        try:
            _switch_path().unlink()
        except OSError:
            pass
        _devices.clear()
        _refusals.clear()
        _rotated_at = 0.0


# ---------------------------------------------------------------------------
# The panel's one read
# ---------------------------------------------------------------------------

def pairing(root: Optional[Path], port: int) -> dict[str, Any]:
    """URL, phone token, project, and the QR payload - one source for the
    terminal print, the /pair page and the Settings panel."""
    from bgate_ui import tailnet as _tailnet
    url = f"http://{host()}:{port}" if host() else ""
    try:
        token = ensure_token()
    except OSError:
        token = ""
    project = root.name if root is not None else "(no project)"
    return {"url": url, "token": token, "project": project,
            "payload": _tailnet.pairing_payload(url, token, project) if url else ""}


def qr_data_uri(payload: str) -> str:
    """The pairing QR as an SVG data URI, or "" when segno is not installed."""
    if not payload:
        return ""
    try:
        import segno
        return segno.make(payload, error="m").svg_data_uri(
            scale=8, border=2, dark="#111", light="#fff")
    except Exception:                                            # noqa: BLE001
        return ""


def status(root: Optional[Path], port: int) -> dict[str, Any]:
    pair = pairing(root, port)
    return {
        "listening": listening(),
        "enabled": enabled(),
        "host": host(),
        **pair,
        "qr": qr_data_uri(pair["payload"]),
        "token_file": str(token_path()),
        "rotated_at": _rotated_at or None,
        "devices": devices(),
        "refusals": refusals(),
        "restart_hint": ("" if listening() else
                         "This server was started without --remote, so the socket "
                         "is loopback-only. Restart it with `bgate serve --remote` "
                         "(or `bgate app --remote`) to let a phone reach it."),
    }
