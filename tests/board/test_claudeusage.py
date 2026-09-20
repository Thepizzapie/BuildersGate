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
    got = claudeusage.usage()
    assert got["weekly"] == {"used_percent": 30, "resets_at": future, "at": 0,
                             "source": "bridge"}
    # expired is kept, without its percentage, so the panel can say "reset"
    # instead of drawing the same dash an unlinked bridge draws
    assert got["five_hour"]["expired"] is True
    assert "used_percent" not in got["five_hour"]


def test_status_says_how_old_the_snapshot_is(monkeypatch, tmp_path):
    _use_home(monkeypatch, tmp_path)
    claudeusage.install()
    claudeusage._write_json(claudeusage._snapshot_path(), {
        "updated_at": int(time.time()) - 7 * 86400,
        "weekly": {"used_percent": 24, "resets_at": int(time.time()) + 100}})
    st = claudeusage.status()
    assert st["stale"] is True and st["age_s"] >= 7 * 86400 - 5


def test_the_director_keeps_its_own_context_and_takes_percentages_from_the_bridge():
    from bgate_ui.agents import directorsession as ds
    session = {"context": {"used": 180481, "limit": 1000000, "at": 200, "source": "session"},
               "five_hour": {"status": "rejected", "resets_at": 999, "at": 200,
                             "source": "session"}}
    bridge = {"context": {"used": 893290, "limit": 1000000, "at": 100, "source": "bridge"},
              "five_hour": {"used_percent": 61, "resets_at": 900, "at": 100, "source": "bridge"},
              "weekly": {"used_percent": 24, "resets_at": 5000, "at": 100, "source": "bridge"}}
    got = ds._merge_claude_usage(session, bridge)
    assert got["context"]["used"] == 180481             # never the bridge's
    assert got["five_hour"]["used_percent"] == 61       # only the bridge knows it
    assert got["five_hour"]["status"] == "rejected"     # the stream is newer
    assert got["five_hour"]["resets_at"] == 999
    assert got["weekly"]["used_percent"] == 24
    assert ds._merge_claude_usage({}, {}) == {}


def test_codex_context_is_the_last_turn_not_the_thread_total():
    """thread/tokenUsage/updated carries `total` (the thread's running sum)
    and `last` (the latest prompt). The context meter drew 730k of a 272k
    window because it read the sum."""
    from bgate_ui.agents import directorsession as ds
    entry = {"record": "", "says": [], "events": [
        {"method": "item/completed",
         "params": {"item": {"type": "agentMessage", "text": "done"}}},
        {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {
            "total": {"inputTokens": 730596, "outputTokens": 9000, "cachedInputTokens": 600000},
            "last": {"inputTokens": 30000, "cachedInputTokens": 120000},
            "modelContextWindow": 272000}}},
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ]}
    got = ds._collect_codex(entry, 0)
    assert got["ok"] and got["text"] == "done"
    assert got["tokens"]["context_used"] == 150000
    assert got["context_limit"] == 272000
