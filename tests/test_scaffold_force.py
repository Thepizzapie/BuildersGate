"""force=True used to be the one unrecoverable mistake this tool offers.

It walked the template tree and wrote every file over whatever was already
there. Someone reaching for it to top up a missing addon lost project.godot,
scripts/player.gd, scenes/main.tscn, .gitignore and export_presets.cfg in
place — and export_presets.cfg is the one you cannot get back, because the
.gitignore this same template stamps excludes it from git. The result dict said
nothing about any of it; "files" listed the writes as if they were creations.

These tests pin the new contract: force fills in gaps, replace is the explicit
overwrite and takes a .bak first, and both report what they touched.
"""
from __future__ import annotations

import pytest

from bgate_core import scaffold

# The files a user is most likely to have customised, and the ones the old
# force run destroyed. export_presets.cfg is first because it is gitignored.
CUSTOMISED = {
    "export_presets.cfg": '[preset.0]\nname="MyAndroid"\n',
    "scripts/player.gd": "extends CharacterBody2D\n# six months of feel tuning\n",
    "project.godot": "; hand-edited input map\n",
    ".gitignore": "# my own ignores\n",
}


def _scaffold_then_customise(tmp_path):
    scaffold.new_project(tmp_path, "Emberfall", kind="2d")
    for rel, text in CUSTOMISED.items():
        (tmp_path / rel).write_text(text, encoding="utf-8")
    return tmp_path


class TestForceDoesNotDestroy:
    def test_force_keeps_every_customised_file(self, tmp_path):
        project = _scaffold_then_customise(tmp_path)
        scaffold.new_project(project, "Emberfall", kind="2d", force=True)
        for rel, text in CUSTOMISED.items():
            assert (project / rel).read_text(encoding="utf-8") == text, rel

    def test_force_still_fills_in_a_missing_file(self, tmp_path):
        """The legitimate reason people reach for force: a lost addon file."""
        project = _scaffold_then_customise(tmp_path)
        addon = project / "addons" / "bgate" / "bgate_telemetry.gd"
        addon.unlink()

        got = scaffold.new_project(project, "Emberfall", kind="2d", force=True)

        assert addon.exists()
        assert "addons/bgate/bgate_telemetry.gd" in got["created"]

    def test_force_reports_what_it_left_alone(self, tmp_path):
        project = _scaffold_then_customise(tmp_path)
        got = scaffold.new_project(project, "Emberfall", kind="2d", force=True)

        skipped = {s["file"] for s in got["skipped"]}
        assert skipped == set(CUSTOMISED)
        assert all(s["reason"] for s in got["skipped"])
        # A caller that prints nothing but files/note must still say something.
        assert got["files"] == []
        assert "export_presets.cfg" in got["note"]

    def test_untouched_files_are_reported_as_unchanged_not_written(self, tmp_path):
        project = _scaffold_then_customise(tmp_path)
        got = scaffold.new_project(project, "Emberfall", kind="2d", force=True)

        assert "scenes/main.tscn" in got["unchanged"]
        assert "scenes/main.tscn" not in got["files"]

    def test_a_second_scaffold_of_a_clean_project_writes_nothing(self, tmp_path):
        """Re-running on an untouched project must not churn every file."""
        scaffold.new_project(tmp_path, "Emberfall", kind="2d")
        got = scaffold.new_project(tmp_path, "Emberfall", kind="2d", force=True)

        assert got["files"] == []
        assert got["skipped"] == []
        assert "project.godot" in got["unchanged"]


class TestReplaceIsExplicitAndBacksUp:
    def test_replace_overwrites_but_leaves_a_bak(self, tmp_path):
        project = _scaffold_then_customise(tmp_path)
        got = scaffold.new_project(project, "Emberfall", kind="2d", replace=True)

        for rel, text in CUSTOMISED.items():
            assert (project / rel).read_text(encoding="utf-8") != text, rel
            assert (project / (rel + ".bak")).read_text(encoding="utf-8") == text

        replaced = {r["file"] for r in got["replaced"]}
        assert replaced == set(CUSTOMISED)
        assert got["skipped"] == []
        assert all(r["backup"] for r in got["replaced"])

    def test_a_second_replace_does_not_clobber_the_first_backup(self, tmp_path):
        """The .bak is the rescue copy; reusing the name would destroy it."""
        project = _scaffold_then_customise(tmp_path)
        scaffold.new_project(project, "Emberfall", kind="2d", replace=True)

        (project / "scripts" / "player.gd").write_text("second edit\n",
                                                       encoding="utf-8")
        scaffold.new_project(project, "Emberfall", kind="2d", replace=True)

        backups = sorted(p.name for p in (project / "scripts").glob("player.gd.bak*"))
        assert backups == ["player.gd.bak", "player.gd.bak.1"]
        first = (project / "scripts" / "player.gd.bak").read_text(encoding="utf-8")
        assert first == CUSTOMISED["scripts/player.gd"]

    def test_replace_alone_enters_a_non_empty_directory(self, tmp_path):
        (tmp_path / "junk.txt").write_text("x", encoding="utf-8")
        assert scaffold.new_project(tmp_path, "Emberfall", replace=True)["ok"]

    def test_neither_flag_still_refuses(self, tmp_path):
        (tmp_path / "my_work.gd").write_text("precious", encoding="utf-8")
        with pytest.raises(FileExistsError, match="not empty"):
            scaffold.new_project(tmp_path, "Emberfall")


class TestFilesOutsideTheTemplateSurvive:
    def test_the_users_own_scripts_are_never_touched(self, tmp_path):
        project = _scaffold_then_customise(tmp_path)
        mine = project / "scripts" / "boss_ai.gd"
        mine.write_text("extends Node\n", encoding="utf-8")

        scaffold.new_project(project, "Emberfall", kind="2d", replace=True)

        assert mine.read_text(encoding="utf-8") == "extends Node\n"
        assert not (project / "scripts" / "boss_ai.gd.bak").exists()


class TestTheAdviceIsActionable:
    """A message telling you to pass something you cannot pass is not advice.

    The skip reason named `replace=True`, the Python keyword argument. It is
    read in two places that cannot use each other's spelling: a terminal user
    gets it from `bgate init` and needs the flag, an agent gets it from
    godot_scaffold and needs the argument. It names both now.
    """

    def test_the_skip_reason_names_the_cli_flag_and_the_api_argument(self, tmp_path):
        dest = tmp_path / "game"
        scaffold.new_project(dest, "Game", kind="2d")
        target = dest / "scripts" / "player.gd"
        target.write_text("# mine\n", encoding="utf-8")
        got = scaffold.new_project(dest, "Game", kind="2d", force=True)
        reason = got["skipped"][0]["reason"]
        assert "--replace" in reason, reason
        assert "replace=True" in reason, reason
