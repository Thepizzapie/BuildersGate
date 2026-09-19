"""Detect this machine's Tailscale address for remote (phone) mode.

100.64.0.0/10 is the CGNAT range Tailscale assigns. We prefer `tailscale ip -4`
and fall back to scanning local interfaces for an address in that range.
"""
from __future__ import annotations

import json
import socket
import subprocess
from dataclasses import dataclass
from ipaddress import ip_address, ip_network

_CGNAT = ip_network("100.64.0.0/10")


@dataclass(frozen=True)
class TailnetAddr:
    ip: str
    name: str | None = None


def is_tailnet_ip(ip: str) -> bool:
    try:
        return ip_address(ip) in _CGNAT
    except ValueError:
        return False


def _iface_addrs() -> list[str]:
    out: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       family=socket.AF_INET):
            out.append(info[4][0])
    except OSError:
        pass
    return out


def _dns_name(runner=subprocess.run) -> str | None:
    """The node's MagicDNS name (<host>.<tailnet>.ts.net), so a
    `tailscale serve` proxy - whose requests arrive with that Host - passes
    the same-origin gate. tailscaled is the only listener that path opens;
    the dashboard process itself stays unreachable from the network, which
    is the point on a machine whose firewall blocks python inbound."""
    try:
        res = runner(["tailscale", "status", "--json"], capture_output=True,
                     text=True, timeout=3)
        name = (json.loads(res.stdout or "{}").get("Self") or {}).get("DNSName") or ""
        return name.rstrip(".") or None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def detect(runner=subprocess.run, interfaces=_iface_addrs) -> TailnetAddr | None:
    try:
        res = runner(["tailscale", "ip", "-4"], capture_output=True,
                     text=True, timeout=3)
        ip = (getattr(res, "stdout", "") or "").strip().splitlines()
        if ip:
            first = ip[0].strip()
            if is_tailnet_ip(first):
                return TailnetAddr(ip=first, name=_dns_name(runner))
    except (OSError, subprocess.SubprocessError):
        pass
    for ip in interfaces():
        if is_tailnet_ip(ip):
            return TailnetAddr(ip=ip)
    return None


def pairing_payload(url: str, token: str, project: str) -> str:
    """JSON contract a companion app scans from the pairing QR: {url, token, project}."""
    return json.dumps({"url": url, "token": token, "project": project})
