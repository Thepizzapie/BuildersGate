"""The sign-off panel's evidence is what the harness recorded, not the note."""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from bgate_core.board import gitwork, proof, queue
from bgate_core.runtime import enginetests

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(root, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", *args], cwd=str(root), env=env, check=True,
                   capture_output=True)


@needs_git
def test_commits_tests_checks_and_images_are_read_from_the_record(root):
    (root / "base.txt").write_text("x", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base", "--no-gpg-sign")
    item = queue.add(root, "level", "slice_dungeon")
    queue.set_status(root, item["id"], "review", result="all four rooms walk")
    qa = queue.add(root, "qa", f"QA gate: verify #{item['id']} - slice_dungeon")
    other = queue.add(root, "level", "unrelated")
    (root / "room.ts").write_text("rooms", encoding="utf-8")
    gitwork.commit_paths(root, ["room.ts"], f"bgate: item #{item['id']} [level] - x")
    (root / "other.ts").write_text("o", encoding="utf-8")
    gitwork.commit_paths(root, ["other.ts"], f"bgate: item #{other['id']}0 [level] - x")

    enginetests.record(root, {"ok": False, "assertions_passed": 9, "assertions_failed": 1,
                              "by": f"agent:item-{qa['id']}",
                              "scripts": [{"script": "src/room.test.ts", "ok": False}]})
    enginetests.record(root, {"ok": True, "by": f"agent:item-{other['id']}"})

    (root / "shot.png").write_bytes(b"\x89PNG")
    log = root / ".bgate" / "agents" / f"item-{item['id']}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join(json.dumps(e) for e in [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__builders-gate__engine_screenshot", "input": {}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": json.dumps({"ok": True, "path": "shot.png"})}]}]}},
    ]), encoding="utf-8")

    got = proof.proof(root, item["id"])
    assert [c["files"][0]["path"] for c in got["commits"]] == ["room.ts"]
    assert len(got["tests"]) == 1 and got["tests"][0]["ok"] is False
    assert got["tests"][0]["failing"] == ["src/room.test.ts"]
    assert got["checks"][0]["tool"] == "engine_screenshot" and got["checks"][0]["ok"] is True
    assert got["images"] == ["shot.png"]
    assert got["claim"] == "all four rooms walk"
    assert "room.ts" in proof.diff(root, got["commits"][0]["sha"])


def test_images_outside_the_project_are_refused(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "x.png"
    outside.write_bytes(b"x")
    with pytest.raises(ValueError):
        proof.image_path(root, str(outside))
    with pytest.raises(ValueError):
        proof.diff(root, "HEAD; rm -rf /")


@needs_git
def test_history_lists_landed_work_oldest_first_with_commits(root):
    from bgate_core.board import mergeorder
    (root / "base.txt").write_text("x", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base", "--no-gpg-sign")
    a = queue.add(root, "tech", "rng")
    b = queue.add(root, "tech", "campaign", depends_on=a["id"])
    for item, name in ((a, "rng.ts"), (b, "campaign.ts")):
        (root / name).write_text("x", encoding="utf-8")
        gitwork.commit_paths(root, [name], f"bgate: item #{item['id']} [tech] - x")
        queue.set_status(root, item["id"], "done")
    open_one = queue.add(root, "tech", "still open")

    got = mergeorder.history(root, hours=0)["landed"]
    assert [r["id"] for r in got] == [a["id"], b["id"]]
    assert got[1]["after"] == [a["id"]]
    assert got[0]["commits"][0]["files"] == 1
    assert open_one["id"] not in {r["id"] for r in got}
