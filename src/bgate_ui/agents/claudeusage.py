"""Opt-in Claude usage bridge.

Claude Code already exposes account limits to status-line commands.  This
bridge captures only those limit fields; it never reads credentials, session
content, project paths, or calls an Anthropic endpoint.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


MARKER = "bgate_cli.statusline"


def _home() -> Path:
    return Path.home()


def _settings_path() -> Path:
    return _home() / ".claude" / "settings.json"


def _state_dir() -> Path:
    return _home() / ".bgate"


def _bridge_path() -> Path:
    return _state_dir() / "claude-statusline.json"


def _snapshot_path() -> Path:
    return _state_dir() / "claude-usage.json"


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _our_command(value: object) -> bool:
    return isinstance(value, dict) and MARKER in str(value.get("command") or "")


def _command() -> str:
    return f'"{sys.executable}" -m {MARKER}'


def status() -> dict:
    settings = _read_json(_settings_path())
    enabled = _our_command(settings.get("statusLine"))
    snapshot = _read_json(_snapshot_path()) if enabled else {}
    return {
        "enabled": enabled,
        "has_snapshot": bool(snapshot.get("context") or snapshot.get("five_hour")
                             or snapshot.get("weekly")),
        "updated_at": snapshot.get("updated_at"),
        "needs_restart": enabled and not bool(snapshot),
    }


def install() -> dict:
    settings_path = _settings_path()
    try:
        parsed = json.loads(settings_path.read_text(encoding="utf-8")) \
            if settings_path.exists() else {}
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("Claude settings are not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Claude settings must be a JSON object")
    settings = parsed
    current = settings.get("statusLine")
    if _our_command(current):
        return status()

    _write_json(_bridge_path(), {"previous": current})
    replacement = {"type": "command", "command": _command()}
    if isinstance(current, dict):
        for key in ("padding", "refreshInterval", "hideVimModeIndicator"):
            if key in current:
                replacement[key] = current[key]
    settings["statusLine"] = replacement
    _write_json(settings_path, settings)
    return status()


def uninstall() -> dict:
    settings_path = _settings_path()
    settings = _read_json(settings_path)
    if _our_command(settings.get("statusLine")):
        bridge = _read_json(_bridge_path())
        if bridge.get("previous") is None:
            settings.pop("statusLine", None)
        else:
            settings["statusLine"] = bridge["previous"]
        _write_json(settings_path, settings)
    for path in (_bridge_path(), _snapshot_path()):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return status()


def previous_command() -> str:
    previous = _read_json(_bridge_path()).get("previous")
    if (isinstance(previous, dict) and previous.get("type") == "command"
            and not _our_command(previous)):
        return str(previous.get("command") or "")
    return ""


def run_previous(raw: str) -> str:
    command = previous_command()
    if not command:
        return ""
    try:
        result = subprocess.run(command, input=raw, text=True, shell=True,
                                capture_output=True, timeout=3)
        return result.stdout.rstrip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _window(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    try:
        percent = max(0, min(100, round(float(value.get("used_percentage")))))
    except (TypeError, ValueError):
        return {}
    out = {"used_percent": percent}
    try:
        reset = int(value.get("resets_at"))
        if reset > 0:
            out["resets_at"] = reset
    except (TypeError, ValueError):
        pass
    return out


def _context(value: object) -> dict:
    """Keep only the numeric context meter from Claude's status payload."""
    if not isinstance(value, dict):
        return {}
    try:
        limit = max(0, int(value.get("context_window_size") or 0))
        used = max(0, int(value.get("total_input_tokens") or 0)
                   + int(value.get("total_output_tokens") or 0))
    except (TypeError, ValueError):
        return {}
    if not used and limit:
        try:
            used = round(limit * float(value.get("used_percentage") or 0) / 100)
        except (TypeError, ValueError):
            used = 0
    return {"used": used, "limit": limit} if limit else {}


def capture(raw: str) -> None:
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    if not isinstance(limits, dict):
        return
    five_hour = _window(limits.get("five_hour"))
    weekly = _window(limits.get("seven_day"))
    context = _context(payload.get("context_window"))
    if not context and not five_hour and not weekly:
        return
    snapshot = {"updated_at": int(time.time())}
    if context:
        snapshot["context"] = context
    if five_hour:
        snapshot["five_hour"] = five_hour
    if weekly:
        snapshot["weekly"] = weekly
    _write_json(_snapshot_path(), snapshot)


def usage() -> dict:
    if not status()["enabled"]:
        return {}
    snapshot = _read_json(_snapshot_path())
    now = int(time.time())
    out = {}
    for key in ("context", "five_hour", "weekly"):
        value = snapshot.get(key)
        if not isinstance(value, dict):
            continue
        reset = value.get("resets_at") if key != "context" else None
        if reset and int(reset) <= now:
            continue
        out[key] = value
    return out
