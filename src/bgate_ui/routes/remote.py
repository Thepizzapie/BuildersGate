"""Settings > Phone: the controls over the companion app's access.

Everything here is LOOPBACK-ONLY, checked on the Host the request asked for,
the same way /pair is: the phone must never be able to read the credential
it is supposed to scan, switch its own door back on, or un-revoke itself.
A tailnet-side request gets a 404, not a 403, so the surface does not even
confirm it exists from that side.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from bgate_ui import api, remote as _remote
from bgate_ui.deps import root as _root

router = APIRouter()


def _root_or_none():
    try:
        return _root()
    except HTTPException:
        return None


def _loopback_only(request: Request) -> None:
    asked = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
    if asked.lower() not in api._LOOPBACK_HOSTS:
        raise HTTPException(404, "not found")


def _status(request: Request) -> dict:
    return _remote.status(_root_or_none(), request.url.port or 7788)


@router.get("/api/remote")
def remote_status(request: Request) -> dict:
    """The switch, the socket, the pairing QR, and every device on it."""
    _loopback_only(request)
    return _status(request)


@router.post("/api/remote/enable")
def remote_enable(request: Request) -> dict:
    """Open the tailnet door. Needs the --remote bind; says so if it is
    missing rather than pretending the switch did something."""
    _loopback_only(request)
    if not _remote.listening():
        raise api.bad_request(
            "this server was not started with --remote, so there is no "
            "tailnet socket to open; restart it with `bgate serve --remote`",
            code="not_listening")
    _remote.set_enabled(True)
    return _status(request)


@router.post("/api/remote/disable")
def remote_disable(request: Request) -> dict:
    """Close the tailnet door without stopping the server. Every phone gets
    403 remote_off on its next poll; the desktop page is untouched."""
    _loopback_only(request)
    _remote.set_enabled(False)
    return _status(request)


@router.post("/api/remote/rotate")
def remote_rotate(request: Request) -> dict:
    """New phone token, new QR. Every paired phone is cut off at once."""
    _loopback_only(request)
    root = _root_or_none()
    if root is None:
        raise api.bad_request("no project is open, so there is no token to rotate")
    _remote.rotate_token(root)
    return _status(request)


@router.post("/api/remote/devices/{device_id}/revoke")
def remote_revoke(device_id: str, request: Request) -> dict:
    """Refuse one device while the others keep working. Sticks until the
    device is restored, forgotten, or the token is rotated."""
    _loopback_only(request)
    if _remote.set_revoked(device_id, True) is None:
        raise api.not_found(f"no device {device_id}", device=device_id)
    return _status(request)


@router.post("/api/remote/devices/{device_id}/restore")
def remote_restore(device_id: str, request: Request) -> dict:
    _loopback_only(request)
    if _remote.set_revoked(device_id, False) is None:
        raise api.not_found(f"no device {device_id}", device=device_id)
    return _status(request)


@router.delete("/api/remote/devices/{device_id}")
def remote_forget(device_id: str, request: Request) -> dict:
    """Drop the row. Not a revoke: a device that still holds the token is
    back on its next request. The panel says so beside the button."""
    _loopback_only(request)
    if not _remote.forget(device_id):
        raise api.not_found(f"no device {device_id}", device=device_id)
    return _status(request)


@router.delete("/api/remote/devices")
def remote_forget_all(request: Request, revoked_only: Optional[bool] = False) -> dict:
    _loopback_only(request)
    if revoked_only:
        for row in _remote.devices():
            if row["revoked"]:
                _remote.forget(row["id"])
    else:
        _remote.forget()
    return _status(request)
