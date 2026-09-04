import json
import time

from bgate_ui.agents import claudeusage


def _use_home(monkeypatch, tmp_path):
    monkeypatch.setattr(claudeusage, "_home", lambda: tmp_path)


def test_capture_keeps_only_quota_fields(monkeypatch, tmp_path):
    _use_home(monkeypatch, tmp_path)
    raw = json.dumps({
        "cwd": "secret-project", "session_id": "secret-session",
        "rate_limits": {
            "five_hour": {"used_percentage": 21.4, "resets_at": 4102444800,
                          "unwanted": "secret-token"},
            "seven_day": {"used_percentage": 52, "resets_at": 4102444801},
        },
        "context_window": {"context_window_size": 200000,
                           "total_input_tokens": 15000,
                           "total_output_tokens": 1200,
                           "current_usage": {"input_tokens": 8500},
                           "unwanted": "private transcript data"},
    })
    claudeusage.capture(raw)
    saved = json.loads((tmp_path / ".bgate" / "claude-usage.json").read_text())
    assert set(saved) == {"updated_at", "context", "five_hour", "weekly"}
    assert saved["context"] == {"used": 16200, "limit": 200000}
    assert saved["five_hour"] == {"used_percent": 21, "resets_at": 4102444800}
    assert "secret" not in json.dumps(saved)


def test_install_and_uninstall_restore_existing_statusline(monkeypatch, tmp_path):
    _use_home(monkeypatch, tmp_path)
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    original = {"theme": "dark", "statusLine": {
        "type": "command", "command": "old-status", "padding": 2}}
    path.write_text(json.dumps(original))

    installed = claudeusage.install()
    configured = json.loads(path.read_text())
    assert installed["enabled"] is True
    assert claudeusage.MARKER in configured["statusLine"]["command"]
    assert configured["statusLine"]["padding"] == 2

    disconnected = claudeusage.uninstall()
    assert disconnected["enabled"] is False
    assert json.loads(path.read_text()) == original
    assert not (tmp_path / ".bgate" / "claude-usage.json").exists()


def test_usage_omits_expired_windows(monkeypatch, tmp_path):
    _use_home(monkeypatch, tmp_path)
    claudeusage.install()
    future = int(time.time()) + 100
    claudeusage._write_json(claudeusage._snapshot_path(), {
        "five_hour": {"used_percent": 80, "resets_at": int(time.time()) - 1},
        "weekly": {"used_percent": 30, "resets_at": future},
    })
    assert claudeusage.usage() == {
        "weekly": {"used_percent": 30, "resets_at": future}}
