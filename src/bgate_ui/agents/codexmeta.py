"""Small client for Codex's installed app-server metadata API.

The CLI already exposes its exact model catalog and account rate-limit windows;
copying either into Builders Gate would make both stale. This module performs a
short initialized JSON-RPC exchange and caches the public metadata. It never
returns account identifiers or credentials.
"""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time

from bgate_ui.agents import runners

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "value": {"models": [], "limits": {}}}
CACHE_S = 60.0


def _rpc(exe: str) -> dict:
    rows = [
        {"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "builders-gate", "version": "1"},
            "capabilities": {"experimentalApi": True}}},
        {"method": "initialized"},
        {"id": 2, "method": "model/list", "params": {
            "includeHidden": False}},
        {"id": 3, "method": "account/rateLimits/read"},
    ]
    try:
        proc = subprocess.Popen(
            [exe, "app-server", "--stdio"], text=True, encoding="utf-8",
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except OSError:
        return {"models": [], "limits": {}}
    replies = {}
    lines: queue.Queue[str] = queue.Queue()

    def read() -> None:
        try:
            for line in proc.stdout or ():
                lines.put(line)
        except (OSError, ValueError):
            pass

    threading.Thread(target=read, daemon=True, name="codex-meta-read").start()
    try:
        assert proc.stdin is not None
        for row in rows:
            proc.stdin.write(json.dumps(row) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + 12
        while len(replies) < 2 and time.monotonic() < deadline:
            try:
                line = lines.get(timeout=.25)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(row, dict) and row.get("id") in (2, 3):
                replies[int(row["id"])] = row.get("result") or {}
    except (OSError, ValueError):
        pass
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=_NO_WINDOW, timeout=5)
            else:
                proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.SubprocessError:
            proc.kill()
    contexts = _context_windows(exe)
    models = []
    for row in (replies.get(2) or {}).get("data") or []:
        if not isinstance(row, dict) or row.get("hidden"):
            continue
        model = str(row.get("model") or row.get("id") or "").strip()
        if model:
            models.append({"value": model,
                           "label": str(row.get("displayName") or model),
                           "description": str(row.get("description") or ""),
                           "default": bool(row.get("isDefault")),
                           "context_limit": int(contexts.get(model) or 0)})
    raw_limits = replies.get(3) or {}
    return {"models": models,
            "limits": raw_limits.get("rateLimitsByLimitId") or {
                "codex": raw_limits.get("rateLimits") or {}}}


def _context_windows(exe: str) -> dict[str, int]:
    """The app-server picker omits this field; Codex's raw catalog owns it."""
    try:
        done = subprocess.run(
            [exe, "debug", "models"], text=True, encoding="utf-8",
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8,
            creationflags=_NO_WINDOW)
        raw = json.loads(done.stdout)
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return {}
    out = {}
    for row in raw.get("models") or []:
        try:
            out[str(row.get("slug") or "")] = int(row.get("context_window") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
    return out


def snapshot(force: bool = False) -> dict:
    now = time.monotonic()
    with _lock:
        if not force and now - float(_cache.get("at") or 0) < CACHE_S:
            return dict(_cache["value"])
        exe = runners.find_codex()
        value = _rpc(exe) if exe else {"models": [], "limits": {}}
        _cache.update(at=now, value=value)
        return dict(value)


def usage_for(model: str) -> dict:
    limits = snapshot().get("limits") or {}
    chosen = limits.get("codex") or {}
    needle = str(model or "").lower().replace("-", "")
    for bucket in limits.values():
        name = str(bucket.get("limitName") or "").lower().replace("-", "")
        if needle and name and (needle in name or name in needle):
            chosen = bucket
            break
    out = {}
    for window in (chosen.get("primary"), chosen.get("secondary")):
        if not isinstance(window, dict):
            continue
        minutes = int(window.get("windowDurationMins") or 0)
        key = "five_hour" if 240 <= minutes <= 360 else \
              "weekly" if 6 * 24 * 60 <= minutes <= 8 * 24 * 60 else ""
        if key:
            out[key] = {"used_percent": int(window.get("usedPercent") or 0),
                        "resets_at": window.get("resetsAt"),
                        "duration_minutes": minutes}
    return out


def context_for(model: str) -> int:
    row = next((r for r in snapshot().get("models") or []
                if r.get("value") == model), {})
    return int(row.get("context_limit") or 0)
