"""In-flight calls: a restart must not lose work without a record."""
from __future__ import annotations

import json
import os

from bgate_core import inflight


def test_a_call_appears_while_it_runs_and_clears_when_it_lands(root):
    token = inflight.begin(root, "image_sprites", seat="art", item_id=12)
    running = inflight.active(root)
    assert [c["tool"] for c in running] == ["image_sprites"]
    assert running[0]["item_id"] == 12
    inflight.end(root, token)
    assert inflight.active(root) == []


def test_a_dead_process_leaves_orphans_not_active_calls(root):
    # A pid that cannot be running: the file is the evidence a server died
    # holding this call.
    here = root / ".bgate" / "inflight"
    here.mkdir(parents=True, exist_ok=True)
    (here / "999999.json").write_text(json.dumps({
        "pid": 999999,
        "calls": {"t1": {"tool": "cinematic_generate_shot", "seat": "cinematic",
                         "item_id": 7, "started": 0.0}}}), encoding="utf-8")
    assert inflight.active(root) == []
    lost = inflight.orphaned(root)
    assert [c["tool"] for c in lost] == ["cinematic_generate_shot"]


def test_the_startup_notice_names_what_died_then_reaps(root):
    here = root / ".bgate" / "inflight"
    here.mkdir(parents=True, exist_ok=True)
    (here / "999998.json").write_text(json.dumps({
        "pid": 999998,
        "calls": {"t1": {"tool": "music_generate", "item_id": 3,
                         "started": 0.0}}}), encoding="utf-8")
    notice = inflight.startup_notice(root)
    assert "music_generate" in notice
    assert "charged" in notice
    # Read once, then gone: the only moment this information exists is the
    # first read after a restart.
    assert inflight.startup_notice(root) == ""


def test_the_restart_warning_ignores_a_fast_call(root, monkeypatch):
    inflight.begin(root, "queue_list")
    assert inflight.restart_warning(root) == ""


def test_the_restart_warning_names_a_slow_one(root):
    here = root / ".bgate" / "inflight"
    here.mkdir(parents=True, exist_ok=True)
    (here / f"{os.getpid()}.json").write_text(json.dumps({
        "pid": os.getpid(),
        "calls": {"t1": {"tool": "blender_generate", "seat": "art",
                         "item_id": 9, "started": 0.0}}}), encoding="utf-8")
    warning = inflight.restart_warning(root)
    assert "WOULD ORPHAN" in warning
    assert "blender_generate" in warning
    assert "still charged" in warning


def test_a_broken_registry_never_fails_a_call(root, monkeypatch):
    monkeypatch.setattr(inflight, "_write",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    # begin() swallows it: a registry that can fail a tool call is a worse bug
    # than the one it exists to record.
    assert inflight.begin(root, "image_generate")
