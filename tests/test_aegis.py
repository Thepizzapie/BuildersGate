"""Aegis - containment, and the path spellings that make it hard.

The interesting tests here are not "inside allows, outside denies". They are the
ways two spellings of ONE location fail to compare equal on Windows - case, drive
letter case, junctions, UNC, the \\\\?\\ extended prefix - because every one of
those is a way an agent pinned to its own project gets told it is trespassing in
its own files, or worse, is let into somebody else's.

Nothing here creates a database. A Builders Gate project, as far as aegis is
concerned, IS the .bgate/game.db marker file, so the fixtures write an empty one
and that is a real project for these purposes.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from bgate_core import aegis, db


def make_project(path: Path) -> Path:
    """The marker aegis looks for, and nothing else."""
    (path / aegis.PROJECT_DIRNAME).mkdir(parents=True, exist_ok=True)
    (path / aegis.PROJECT_DIRNAME / aegis.PROJECT_DBNAME).write_bytes(b"")
    return path


@pytest.fixture
def game(tmp_path: Path) -> Path:
    return make_project(tmp_path / "ember")


@pytest.fixture
def other(tmp_path: Path) -> Path:
    return make_project(tmp_path / "hollow")


def verdict(*args, **kwargs) -> str:
    # tmp_path lives UNDER the system temp dir, which the real allowlist allows.
    # Every test that means "outside" would otherwise come back allowed, so the
    # allowlist is empty unless a test is specifically about it.
    kwargs.setdefault("allowlist", [])
    return aegis.decide(*args, **kwargs)["verdict"]


class TestBasics:
    def test_inside_pinned_root_allows(self, game):
        assert verdict(game, game / "game" / "scripts" / "player.gd") == aegis.ALLOW

    def test_target_that_is_the_root_allows(self, game):
        assert verdict(game, game) == aegis.ALLOW

    def test_different_project_denies_and_names_both(self, game, other):
        out = aegis.decide(game, other / "game" / "player.gd", seat="gameplay",
                           allowlist=[])
        assert out["verdict"] == aegis.DENY
        assert str(game) in out["reason"] and str(other) in out["reason"]
        assert "gameplay" in out["reason"]

    def test_outside_every_project_is_outside_not_deny(self, game, tmp_path):
        assert verdict(game, tmp_path / "loose" / "notes.txt") == aegis.OUTSIDE

    def test_no_pinned_root_is_unscoped_and_allowed(self, other):
        out = aegis.decide("", other / "game" / "player.gd")
        assert out["verdict"] == aegis.ALLOW
        assert out["scope"] == "unscoped"

    def test_scope_is_the_pinned_root(self, game, tmp_path):
        assert aegis.decide(game, tmp_path / "x", allowlist=[])["scope"] == str(
            aegis._resolve(game))

    def test_is_allowed_treats_outside_as_refused(self, game, tmp_path):
        assert not aegis.is_allowed(aegis.decide(game, tmp_path / "x", allowlist=[]))
        assert aegis.is_allowed(aegis.decide(game, game / "x", allowlist=[]))

    def test_sibling_with_a_shared_prefix_is_not_inside(self, tmp_path):
        # C:\games\ember-old startswith C:\games\ember as a plain string. It is
        # not inside it, and a prefix test that says so hands over a whole
        # project by way of a naming convention.
        game = make_project(tmp_path / "ember")
        make_project(tmp_path / "ember-old")
        assert verdict(game, tmp_path / "ember-old" / "player.gd") == aegis.DENY


class TestNonexistentPaths:
    """A write CREATES the file. Judging the intended location is the whole job."""

    def test_missing_file_in_the_project_allows(self, game):
        target = game / "does" / "not" / "exist" / "yet.gd"
        assert not target.exists()
        assert verdict(game, target) == aegis.ALLOW

    def test_missing_file_in_another_project_denies(self, game, other):
        assert verdict(game, other / "not" / "there" / "yet.gd") == aegis.DENY

    def test_missing_root_still_judged(self, tmp_path):
        ghost = tmp_path / "ghost"          # never created
        assert verdict(ghost, ghost / "a.gd") == aegis.ALLOW
        assert verdict(ghost, tmp_path / "elsewhere" / "a.gd") == aegis.OUTSIDE


class TestRelative:
    def test_relative_resolved_against_cwd(self, game):
        assert verdict(game, "game/scripts/player.gd", cwd=game) == aegis.ALLOW

    def test_relative_climbing_out_lands_in_the_other_project(self, game, other):
        rel = os.path.join("..", other.name, "player.gd")
        assert verdict(game, rel, cwd=game) == aegis.DENY

    def test_relative_without_cwd_is_refused_not_guessed(self, game):
        # Resolving against THIS process's cwd would be a guess about a
        # different process's cwd, which is how a relative write lands in the
        # wrong project without anyone noticing.
        out = aegis.decide(game, "player.gd", allowlist=[])
        assert out["verdict"] == aegis.OUTSIDE
        assert "relative" in out["reason"]

    def test_dot_segments_collapse(self, game):
        assert verdict(game, game / "game" / ".." / "design" / "x.md") == aegis.ALLOW


class TestWindowsSpellings:
    def test_case_insensitive_on_windows(self, game):
        shouty = Path(str(game).upper()) / "PLAYER.GD"
        expected = aegis.ALLOW if sys.platform == "win32" else aegis.OUTSIDE
        assert verdict(game, shouty) == expected

    @pytest.mark.skipif(sys.platform != "win32", reason="drive letters are Windows")
    def test_drive_letter_case_does_not_split_a_project(self, game):
        raw = str(game)
        flipped = raw[0].swapcase() + raw[1:]
        assert verdict(flipped, game / "player.gd") == aegis.ALLOW

    @pytest.mark.skipif(sys.platform != "win32", reason="\\\\?\\ is Windows")
    def test_extended_length_prefix_is_the_same_location(self, game):
        assert verdict(game, "\\\\?\\" + str(game / "player.gd")) == aegis.ALLOW
        assert verdict("\\\\?\\" + str(game), game / "player.gd") == aegis.ALLOW

    @pytest.mark.skipif(sys.platform != "win32", reason=(
        "UNC is a Windows spelling. On POSIX \\server\\share is not a path at "
        "all, it is one filename containing backslashes, so there is no "
        "containment question here to answer."))
    def test_unc_paths_compare_as_one_location(self):
        # No share is mounted in CI, so this exercises the comparison itself:
        # containment is decided before anything touches the filesystem.
        unc = Path(r"\\build01\games\ember")
        assert verdict(unc, unc / "game" / "player.gd") == aegis.ALLOW
        assert verdict(unc, Path(r"\\build01\games\hollow\player.gd")) == aegis.OUTSIDE

    def test_extended_unc_equals_plain_unc(self):
        assert aegis._key(Path(r"\\?\UNC\build01\games\ember")) == aegis._key(
            Path(r"\\build01\games\ember"))


def _junction(link: Path, target: Path) -> bool:
    """A directory junction, which Windows allows without administrator rights
    (a symlink does not). Returns False if the OS would not make one."""
    try:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


class TestLinks:
    """A link is a second name for a location. Judging the name rather than the
    location is how an agent walks into another project through a shortcut."""

    @pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows")
    def test_junction_into_another_project_is_denied(self, game, other):
        link = game / "shortcut"
        if not _junction(link, other):
            pytest.skip("this machine will not create junctions")
        assert verdict(game, link / "player.gd") == aegis.DENY

    def test_symlink_into_another_project_is_denied(self, game, other):
        link = game / "linked"
        try:
            link.symlink_to(other, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("no permission to create symlinks here")
        assert verdict(game, link / "player.gd") == aegis.DENY

    def test_symlink_out_of_the_project_is_outside(self, game, tmp_path):
        loose = tmp_path / "loose"
        loose.mkdir()
        link = game / "out"
        try:
            link.symlink_to(loose, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("no permission to create symlinks here")
        assert verdict(game, link / "notes.txt") == aegis.OUTSIDE


class TestWorktrees:
    """Dispatch puts each item's worktree at <root>/.bgate/work/item-<id>, INSIDE
    the project, so containment needs no special case for them. This is the test
    that says so, because the day they move outside the root every dispatched
    agent starts being denied its own working tree."""

    def test_worktree_path_is_inside(self, game):
        work = game / ".bgate" / "work" / "item-7"
        assert verdict(game, work / "game" / "player.gd") == aegis.ALLOW

    def test_another_projects_worktree_is_still_another_project(self, game, other):
        work = other / ".bgate" / "work" / "item-9" / "player.gd"
        assert verdict(game, work) == aegis.DENY


class TestAllowlist:
    def test_allowlisted_directory_allows(self, game, tmp_path):
        toolchain = tmp_path / "toolchain"
        assert aegis.decide(game, toolchain / "blender.log",
                            allowlist=[toolchain])["verdict"] == aegis.ALLOW

    def test_a_project_inside_an_allowlisted_dir_is_still_denied(self, game, tmp_path):
        # The scratch project lives under ~/.bgate, which is allowlisted. Being
        # somewhere allowed does not make somebody else's game fair game.
        home = tmp_path / "bgate-home"
        scratch = make_project(home / "scratch")
        assert aegis.decide(game, scratch / "out.png",
                            allowlist=[home])["verdict"] == aegis.DENY

    def test_default_allowlist_covers_the_system_temp_dir(self, game):
        import tempfile
        target = Path(tempfile.gettempdir()) / "bgate-probe.tmp"
        assert aegis.decide(game, target)["verdict"] == aegis.ALLOW

    def test_bgate_home_override_is_honoured(self, game, tmp_path, monkeypatch):
        home = tmp_path / "elsewhere-bgate"
        monkeypatch.setenv("BGATE_HOME", str(home))
        assert aegis.decide(game, home / "projects.json")["verdict"] == aegis.ALLOW


class TestPurity:
    def test_decide_reads_no_environment_when_given_an_allowlist(
            self, game, other, monkeypatch):
        # The contract two processes rely on: same arguments, same answer, with
        # nothing in the environment able to change it.
        for var in ("BGATE_ROOT", "BGATE_SEAT", "BGATE_HOME", "CLAUDE_CONFIG_DIR"):
            monkeypatch.setenv(var, str(other))
        assert verdict(game, other / "player.gd") == aegis.DENY
        assert verdict(game, game / "player.gd") == aegis.ALLOW

    def test_marker_constants_still_match_the_database_layer(self):
        # aegis hardcodes the marker rather than importing db, to stay cheap on
        # the hook's hot path. This is the tripwire for that duplication.
        assert aegis.PROJECT_DIRNAME == db.DB_DIRNAME
        assert aegis.PROJECT_DBNAME == db.DB_FILENAME

    def test_no_marker_is_written_by_deciding(self, tmp_path):
        loose = tmp_path / "loose"
        loose.mkdir()
        aegis.decide(tmp_path / "pinned", loose / "a.txt", allowlist=[])
        assert list(loose.iterdir()) == []
        assert not (loose / aegis.PROJECT_DIRNAME).exists()
