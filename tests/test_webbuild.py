"""Exporting the Web build, and telling the truth about whether it worked.

The bug these cover, in full: `rebuild` checked only that a pck EXISTED after
running Godot. A failed export leaves the PREVIOUS build exactly where it was,
so a rebuild that could not run reported ok, handed back the old file's size,
and the panel went on saying the build was behind. From the outside that is
"the button does nothing".

Observed with a real cause - Godot 4.4.1 with no Web export templates installed
exits 1 and writes nothing - and it returned {"ok": true, "bytes": 45463700}
for a pck five days old.
"""
from __future__ import annotations

import subprocess

import pytest

from bgate_ui import webbuild


class _Result:
    def __init__(self, code: int, err: str = "", out: str = ""):
        self.returncode = code
        self.stderr = err
        self.stdout = out


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A root that looks exportable, so rebuild() gets as far as running Godot."""
    game = tmp_path / "game"
    game.mkdir()
    (game / "project.godot").write_text("[application]\n", encoding="utf-8")
    (game / "export_presets.cfg").write_text("[preset.0]\n", encoding="utf-8")
    monkeypatch.setattr(webbuild, "_godot", lambda: "godot")
    return tmp_path


def test_a_failed_export_is_not_reported_as_a_build(project, monkeypatch):
    """The regression. A stale pck is left behind by a failed export, and its
    presence must not be read as success."""
    out = project / "export" / "web"
    out.mkdir(parents=True)
    stale = out / "index.pck"
    stale.write_bytes(b"an old build")

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result(
        1, "ERROR: Cannot export project with preset \"Web\"\n"
           "No export template found at the expected path:\n"
           "  .../web_nothreads_release.zip\n"))

    res = webbuild.rebuild(project)
    assert res["ok"] is False, "a failed export was reported as a build"
    assert res["returncode"] == 1
    # And it names the fix rather than saying "export failed".
    assert "export template" in res["error"].lower()
    assert stale.read_bytes() == b"an old build", "the old build was disturbed"


def test_success_with_no_new_bytes_is_still_a_failure(project, monkeypatch):
    """A zero exit is not the only way to write nothing.

    The question the caller is actually asking is "is there a NEW build" - it is
    about to serve the result to a playtester - so an unchanged mtime is a
    failure however cheerfully the exporter exited.
    """
    out = project / "export" / "web"
    out.mkdir(parents=True)
    (out / "index.pck").write_bytes(b"unchanged")

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result(0))

    res = webbuild.rebuild(project)
    assert res["ok"] is False
    assert "no new build" in res["error"]


def test_a_real_export_is_reported_as_one(project, monkeypatch):
    out = project / "export" / "web"
    out.mkdir(parents=True)
    (out / "index.pck").write_bytes(b"old")
    old_mtime = (out / "index.pck").stat().st_mtime

    def fake_run(*a, **k):
        pck = out / "index.pck"
        pck.write_bytes(b"a genuinely new build")
        import os
        os.utime(pck, (old_mtime + 10, old_mtime + 10))
        return _Result(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    res = webbuild.rebuild(project)
    assert res["ok"] is True, res
    assert res["bytes"] == len(b"a genuinely new build")


def test_the_export_error_names_the_fix_when_godot_names_it():
    missing = ("ERROR: Cannot export project with preset \"Web\" due to "
               "configuration errors:\nNo export template found at the "
               "expected path:\n  web_nothreads_release.zip\n")
    assert "Manage Export Templates" in webbuild._export_error(missing)

    # Anything else: Godot's own first ERROR line, which is more useful than a
    # sentence this module invents.
    other = "ERROR: Project export for preset \"Web\" failed.\n   at: x.cpp:1\n"
    assert webbuild._export_error(other).startswith("Project export")
    assert webbuild._export_error("") == ""
