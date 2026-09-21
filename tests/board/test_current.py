"""GRIPE 38 (EXIT 67 postmortem, 2026-09-21): agents had no sense of what is
CURRENT. current.current_activity() names files changed outside the board
(a human's own commit, or anything uncommitted) versus the harness's own
"item #N ..." auto-commits, and dispatch._current_block()/seats.brief()'s
`current` field surface it.
"""
from __future__ import annotations

import subprocess

from bgate_core.board import current, queue


def _git(root, *args):
    subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                    check=True)


def _init_repo(root):
    _git(root, "init", "-q")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-qm", "initial", "--no-gpg-sign")


class TestNoGit:
    def test_no_repo_is_reported_honestly(self, root):
        got = current.current_activity(root)
        assert got["available"] is False
        assert current.summary_text(root) == ""


class TestOutsideTheBoard:
    def test_a_human_commit_is_flagged_outside_the_board(self, root):
        _init_repo(root)
        (root / "design").mkdir(exist_ok=True)
        (root / "design" / "notes.md").write_text("hand-edited", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "-qm", "quick fix, did it myself", "--no-gpg-sign")

        got = current.current_activity(root, hours=6)
        assert got["available"] is True
        assert "design/notes.md" in got["outside_the_board"]
        assert "director" in got["by_lane"]

    def test_a_harness_commit_is_not_flagged_outside_the_board(self, root):
        _init_repo(root)
        item = queue.add(root, "tech", "small fix")
        (root / "game").mkdir(exist_ok=True)
        (root / "game" / "x.gd").write_text("extends Node", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "-qm", f"item #{item['id']} small fix", "--no-gpg-sign")

        got = current.current_activity(root, hours=6)
        assert "game/x.gd" not in got["outside_the_board"]
        assert "tech" in got["by_lane"]

    def test_uncommitted_work_counts_as_outside_the_board(self, root):
        _init_repo(root)
        (root / "design").mkdir(exist_ok=True)
        (root / "design" / "dirty.md").write_text("wip", encoding="utf-8")

        got = current.current_activity(root, hours=6)
        assert "design/dirty.md" in got["outside_the_board"]

    def test_summary_text_names_the_outside_edit(self, root):
        _init_repo(root)
        (root / "design").mkdir(exist_ok=True)
        (root / "design" / "notes.md").write_text("hand-edited", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "-qm", "quick fix", "--no-gpg-sign")

        text = current.summary_text(root, hours=6)
        assert "changed outside the board" in text
        assert "design/notes.md" in text

    def test_bounded(self, root):
        _init_repo(root)
        for i in range(40):
            p = root / "design" / f"f{i}.md"
            p.parent.mkdir(exist_ok=True)
            p.write_text("x", encoding="utf-8")
        got = current.current_activity(root, hours=6)
        assert len(got["outside_the_board"]) <= current.MAX_FILES_LISTED
