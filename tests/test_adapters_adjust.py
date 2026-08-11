"""Adapter-layer corrections: error parsing, version-ordered discovery, temp
dirs, and the .env cache.

The Godot samples below are VERBATIM captures from Godot 4.7.1 on this machine
(ANSI escapes and all) — not invented strings. Inventing engine output is how
the substring grep this file replaces got written in the first place: it matched
a shape nobody had actually looked at. The live tests re-run the same scenarios
against the real binaries when they are installed, so a future engine that
changes its format fails here instead of in production.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from bgate_adapters import blender, godot
from bgate_core import envfile

# --------------------------------------------------------------------------
# Real Godot 4.7.1 output. Captured 2026-07-25, Windows.
# --------------------------------------------------------------------------

# A clean `--script` run whose game code prints two phrases the OLD grep flagged
# as build errors ("invalid", "error:"). This project is healthy.
HEALTHY_SCRIPT_RUN = (
    "Godot Engine v4.7.1.stable.official.a13da4feb - https://godotengine.org\n"
    "\n"
    "BGATE_OK from Godot\n"
    "checking for invalid input handling\n"
    "no error: everything nominal\n"
)

# A clean `--headless --path <project> --import` of a project with one scene and
# one script. Progress lines arrive ANSI-colored.
HEALTHY_IMPORT = (
    "Godot Engine v4.7.1.stable.official.a13da4feb - https://godotengine.org\n"
    "\n"
    "[   0% ] \x1b[90m\x1b[1mfirst_scan_filesystem\x1b[22m | Started Project "
    "initialization (5 steps)\x1b[39m\x1b[0m\n"
    "[  33% ] \x1b[90m\x1b[1mfirst_scan_filesystem\x1b[22m | Verifying "
    "GDExtensions...\x1b[39m\x1b[0m\n"
    "[  50% ] \x1b[90m\x1b[1mfirst_scan_filesystem\x1b[22m | Creating autoload "
    "scripts...\x1b[39m\x1b[0m\n"
    "\x1b[92m[ DONE ]\x1b[39m \x1b[1mfirst_scan_filesystem\x1b[22m\n"
    "\x1b[0m\n"
)

# The same import against a project with a missing autoload script and a corrupt
# .tscn. Note Godot exits 0 — the errors are the ONLY signal.
FAILING_IMPORT = (
    "Godot Engine v4.7.1.stable.official.a13da4feb - https://godotengine.org\n"
    "\n"
    "[  83% ] \x1b[90m\x1b[1mfirst_scan_filesystem\x1b[22m | Starting file "
    "scan...\x1b[39m\x1b[0m\n"
    "ERROR: Attempt to open script 'res://ghost.gd' resulted in error 'File not found'.\n"
    "   at: load_source_code (modules/gdscript/gdscript.cpp:1139)\n"
    "ERROR: Failed loading resource: res://ghost.gd.\n"
    "   at: _load (core/io/resource_loader.cpp:317)\n"
    "ERROR: Failed to create an autoload, can't load from UID or path: res://ghost.gd.\n"
    "   at: _create_autoload (editor/settings/editor_autoload_settings.cpp:336)\n"
    "ERROR: res://corrupt.tscn:3 - Parse Error: Unexpected end of file.\n"
    "   at: _printerr (scene/resources/resource_format_text.cpp:41)\n"
    "ERROR: Condition \"error != OK\" is true.\n"
    "   at: get_dependencies (scene/resources/resource_format_text.cpp:929)\n"
)

# A GDScript that calls a function that does not exist.
FAILING_SCRIPT_RUN = (
    "SCRIPT ERROR: Parse Error: Function "
    "\"undefined_function_that_does_not_exist()\" not found in base self.\n"
    "   at: GDScript::reload (C:/Users/marta/AppData/Local/Temp/tmp5mygkxgr/s.gd:4)\n"
    "ERROR: Failed to load script "
    "\"C:/Users/marta/AppData/Local/Temp/tmp5mygkxgr/s.gd\" with error \"Parse error\".\n"
    "   at: load (modules/gdscript/gdscript_resource_format.cpp:46)\n"
)

# A failed `load()` at runtime — the engine keeps going and exits 0.
FAILING_LOAD = (
    "ERROR: Cannot open file 'res://does_not_exist.tscn'.\n"
    "   at: load (scene/resources/resource_format_text.cpp:1442)\n"
    "   GDScript backtrace (most recent call first):\n"
    "       [0] _init (C:/Users/marta/AppData/Local/Temp/tmpk2n28lgw/s.gd:4)\n"
    "ERROR: Failed loading resource: res://does_not_exist.tscn.\n"
    "   at: _load (core/io/resource_loader.cpp:317)\n"
)

# VERBATIM tail of `--headless --path <project> --import` on 4.4.1-stable with
# one .glb in the project. The import SUCCEEDED — this is the editor's
# thumbnail step failing to read a texture back out of the headless renderer.
HEADLESS_GLB_IMPORT = (
    "Godot Engine v4.4.1.stable.official.49a5bc7b6 - https://godotengine.org\n"
    "\n"
    "reimport: begin: (Re)Importing Assets steps: 1\n"
    "\treimport: step 0: shard.glb\n"
    "import: begin: Import Scene steps: 104\n"
    "\timport: step 104: Saving...\n"
    "import: end\n"
    "ERROR: Parameter \"t\" is null.\n"
    "   at: texture_2d_get (servers/rendering/dummy/storage/texture_storage.h:107)\n"
)

GODOT = pytest.mark.skipif(not godot.available()["available"],
                           reason="Godot not installed")
BLENDER = pytest.mark.skipif(not blender.available()["available"],
                             reason="Blender not installed")


class TestGodotErrorParsing:
    """The gate godot_check_project hangs off. It must not cry wolf, and it
    must not go quiet."""

    def test_healthy_script_run_reports_nothing(self):
        """'invalid' and 'error:' appear in ordinary game prints. Both of these
        lines made the old grep call a passing run a failing build."""
        assert godot._errors(HEALTHY_SCRIPT_RUN) == []

    def test_healthy_import_reports_nothing(self):
        assert godot._errors(HEALTHY_IMPORT) == []

    def test_failing_import_is_still_caught(self):
        hits = godot._errors(FAILING_IMPORT)
        assert len(hits) == 5
        assert hits[0].startswith("ERROR: Attempt to open script 'res://ghost.gd'")
        assert any("corrupt.tscn:3 - Parse Error" in h for h in hits)

    def test_script_error_is_still_caught(self):
        hits = godot._errors(FAILING_SCRIPT_RUN)
        assert hits[0].startswith("SCRIPT ERROR: Parse Error:")
        assert any("Failed to load script" in h for h in hits)

    def test_failed_load_is_still_caught(self):
        hits = godot._errors(FAILING_LOAD)
        assert len(hits) == 2
        assert "Cannot open file 'res://does_not_exist.tscn'." in hits[0]

    def test_location_and_backtrace_lines_are_not_separate_errors(self):
        """'   at: ...' and the GDScript backtrace belong to the error above
        them; counting them would triple the noise and push real distinct
        errors past the 20-item cap."""
        hits = godot._errors(FAILING_LOAD)
        assert not any(h.startswith("at:") or h.startswith("[0]") for h in hits)

    def test_warnings_are_not_errors(self):
        """Godot warns constantly on healthy projects. A warning must never
        flip check_project's ok flag."""
        output = ("WARNING: Node 'Player' has no shape.\n"
                  "   at: _notification (scene/2d/node_2d.cpp:1)\n"
                  "USER WARNING: deprecated call\n")
        assert godot._errors(output) == []

    def test_colored_error_labels_are_still_matched(self):
        """--import paints its output; an error can arrive wrapped in escapes."""
        assert godot._errors("\x1b[91mERROR: Cannot open file 'res://x.tscn'."
                             "\x1b[0m\n") == [
            "ERROR: Cannot open file 'res://x.tscn'."]

    def test_headless_thumbnail_noise_is_not_a_failed_import(self):
        """Godot 4.4 regressed: importing ANY glTF headlessly prints
        `Parameter "t" is null.` from the dummy renderer, because the scene
        importer's editor-thumbnail step reads a viewport texture back and the
        headless RenderingServer owns none (godotengine/godot#108994).

        The import itself is fine — measured content-blind, a materialless
        untextured cube does it too, and the resource loads with full geometry.
        Counting it made every 3D project with a .glb report a failing build.
        """
        assert godot._errors(HEADLESS_GLB_IMPORT) == []

    def test_the_same_message_from_a_real_renderer_is_still_fatal(self):
        """The suppression is pinned to the dummy-renderer frame, and this is
        why. The screenshot capture runs the game WITHOUT --headless precisely
        because this error there means no PNG was written at all — a genuine
        failure that must never be swallowed along with the thumbnail noise.
        """
        real = ("ERROR: Parameter \"t\" is null.\n"
                "   at: texture_2d_get (servers/rendering/renderer_rd/"
                "storage_rd/texture_storage.cpp:1428)\n")
        assert godot._errors(real) == ['ERROR: Parameter "t" is null.']
        # ...and so is the same message with no frame under it at all.
        assert godot._errors("ERROR: Parameter \"t\" is null.\n") == [
            'ERROR: Parameter "t" is null.']

    def test_duplicates_collapse_and_the_list_is_capped(self):
        assert godot._errors("ERROR: same\n" * 3) == ["ERROR: same"]
        assert len(godot._errors(
            "".join(f"ERROR: distinct {i}\n" for i in range(50)))) == 20


@GODOT
class TestGodotErrorParsingLive:
    """Against the installed engine, not a fixture — if 4.x ever changes the
    format, this is where it must break."""

    def test_healthy_run_with_scary_prints_is_clean(self):
        got = godot.run_script(
            'extends SceneTree\n\n'
            'func _init():\n'
            '\tprint("checking for invalid input handling")\n'
            '\tprint("no error: everything nominal")\n'
            '\tquit()\n', timeout=120)
        assert got["ok"] is True, got
        assert got["errors"] == [], got["errors"]

    def test_broken_script_still_fails(self):
        got = godot.run_script(
            'extends SceneTree\n\n'
            'func _init():\n'
            '\tvar x = undefined_function_that_does_not_exist()\n'
            '\tquit()\n', timeout=120)
        assert got["ok"] is False
        assert any("SCRIPT ERROR" in e for e in got["errors"]), got["errors"]

    def test_healthy_project_imports_clean(self, tmp_path):
        (tmp_path / "project.godot").write_text(
            'config_version=5\n\n[application]\n\nconfig/name="Probe"\n'
            'config/features=PackedStringArray("4.7")\n', encoding="utf-8")
        (tmp_path / "main.gd").write_text(
            'extends Node\n\nfunc _ready() -> void:\n\tprint("hello")\n',
            encoding="utf-8")
        got = godot.check_project(str(tmp_path), timeout=240)
        assert got["ok"] is True, got
        assert got["errors"] == []

    def test_broken_project_is_reported(self, tmp_path):
        """Missing autoload + corrupt scene. Godot exits 0 here — the errors
        list is the entire signal, so it had better still find them."""
        (tmp_path / "project.godot").write_text(
            'config_version=5\n\n[application]\n\nconfig/name="Bad"\n'
            'config/features=PackedStringArray("4.7")\n\n'
            '[autoload]\n\nGhost="*res://ghost.gd"\n', encoding="utf-8")
        (tmp_path / "corrupt.tscn").write_text(
            '[gd_scene format=3]\n\n[node name= type=Node]\ngarbage!!!\n',
            encoding="utf-8")
        got = godot.check_project(str(tmp_path), timeout=240)
        assert got["ok"] is False, got
        assert any("ghost.gd" in e for e in got["errors"]), got["errors"]


class TestBlenderDiscoveryOrder:
    """Lexicographic sort picked 4.10 < 4.9 and had no floor at all."""

    @pytest.fixture()
    def installs(self, tmp_path, monkeypatch):
        """Fake install tree in Blender's real Windows layout."""
        monkeypatch.delenv("BGATE_BLENDER", raising=False)
        monkeypatch.setattr(blender.shutil, "which", lambda name: None)

        def make(*versions: str) -> None:
            for ver in versions:
                exe = tmp_path / f"Blender {ver}" / "blender.exe"
                exe.parent.mkdir(parents=True, exist_ok=True)
                exe.write_bytes(b"stub")
            monkeypatch.setattr(
                blender, "_SEARCH_GLOBS",
                (str(tmp_path / "Blender *" / "blender.exe"),))
        return make

    def test_parses_the_version_out_of_the_install_dir(self, tmp_path):
        assert blender._path_version(
            str(tmp_path / "Blender 4.5" / "blender.exe")) == (4, 5)
        assert blender._path_version("/opt/blender-4.5.1/blender") == (4, 5, 1)
        assert blender._path_version("/usr/bin/blender") == ()

    def test_4_10_beats_4_9(self, installs):
        """The whole bug in one assert: "4.10" < "4.9" as strings."""
        installs("4.9", "4.10")
        assert "Blender 4.10" in blender.find_blender()

    def test_4_5_beats_3_6(self, installs):
        installs("4.5", "3.6")
        assert "Blender 4.5" in blender.find_blender()

    def test_below_floor_install_is_unavailable_with_its_version(self, installs):
        installs("4.1")
        with pytest.raises(blender.BlenderNotFound) as exc:
            blender.find_blender()
        message = str(exc.value)
        assert "4.1" in message, message          # what you have
        assert "4.2" in message, message          # what you need
        assert "not found" not in message.lower()  # it IS installed; say so
        assert blender.available()["available"] is False
        assert "4.1" in blender.available()["reason"]

    def test_a_newer_install_wins_over_a_too_old_one(self, installs):
        installs("4.1", "4.5")
        assert "Blender 4.5" in blender.find_blender()

    def test_unversioned_layout_is_a_fallback_not_a_winner(self, tmp_path,
                                                           monkeypatch):
        """No version in the path means we cannot judge it without spawning the
        binary — usable, but a known-good 4.5 beats it."""
        monkeypatch.delenv("BGATE_BLENDER", raising=False)
        monkeypatch.setattr(blender.shutil, "which", lambda name: None)
        plain = tmp_path / "custom" / "blender.exe"
        plain.parent.mkdir(parents=True)
        plain.write_bytes(b"stub")
        versioned = tmp_path / "Blender 4.5" / "blender.exe"
        versioned.parent.mkdir(parents=True)
        versioned.write_bytes(b"stub")

        monkeypatch.setattr(blender, "_SEARCH_GLOBS", (str(plain),))
        assert blender.find_blender() == str(plain)

        monkeypatch.setattr(blender, "_SEARCH_GLOBS",
                            (str(plain), str(versioned)))
        assert blender.find_blender() == str(versioned)

    def test_floor_matches_the_doctor(self):
        """doctor.py declares the same minimum. Two sources of truth that can
        disagree about one binary is worse than either being wrong."""
        from bgate_core import doctor

        assert blender._pretty(blender.MIN_VERSION) == \
            doctor.MIN_REQUIRED["blender"]


@BLENDER
class TestBlenderDiscoveryLive:
    def test_the_real_install_is_found_and_above_the_floor(self):
        path = blender.find_blender()
        found = blender._path_version(path)
        # PATH installs carry no version in the name; only assert when they do.
        if found:
            assert found >= blender.MIN_VERSION, path

    def test_the_binary_agrees_with_the_path_it_was_found_at(self):
        """Guards _path_version against a layout whose dir name lies."""
        got = blender.version()
        found = blender._path_version(got["path"])
        if found:
            assert got["version"].split()[1].startswith(
                blender._pretty(found[:2]))


def _scratch(prefix: str) -> set[str]:
    return {p.name for p in Path(tempfile.gettempdir()).glob(prefix + "*")}


class TestTempDirCleanup:
    """Scratch dirs used to survive every failure — and failure is what an
    agent produces in bulk while iterating."""

    def test_godot_run_script_cleans_up_after_a_timeout(self, monkeypatch):
        before = _scratch("bgate_godot_")
        monkeypatch.setattr(godot, "find_godot", lambda *a, **k: "godot.exe")

        def spy(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(godot.subprocess, "run", spy)
        got = godot.run_script("extends SceneTree", timeout=1)
        assert got["ok"] is False
        assert _scratch("bgate_godot_") == before

    def test_godot_run_script_cleans_up_after_success(self, monkeypatch):
        before = _scratch("bgate_godot_")
        monkeypatch.setattr(godot, "find_godot", lambda *a, **k: "godot.exe")
        monkeypatch.setattr(
            godot.subprocess, "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "done", ""))
        assert godot.run_script("extends SceneTree", timeout=1)["ok"] is True
        assert _scratch("bgate_godot_") == before

    def test_godot_inspect_cleans_up_after_a_timeout(self, monkeypatch, tmp_path):
        before = _scratch("bgate_inspect_")
        monkeypatch.setattr(godot, "find_godot", lambda *a, **k: "godot.exe")

        def spy(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(godot.subprocess, "run", spy)
        got = godot.inspect_resource(str(tmp_path), "res://x.tscn", timeout=1)
        assert got["ok"] is False
        assert _scratch("bgate_inspect_") == before

    def test_blender_run_script_cleans_up_after_a_timeout(self, monkeypatch,
                                                          tmp_path):
        before = _scratch("bgate_blender_")
        monkeypatch.setattr(blender, "find_blender", lambda: "blender.exe")

        def spy(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(blender.subprocess, "run", spy)
        got = blender.run_script("pass", out_dir=str(tmp_path), timeout=1)
        assert got["ok"] is False
        assert _scratch("bgate_blender_") == before

    def test_blender_run_script_cleans_up_when_blender_writes_nothing(
            self, monkeypatch, tmp_path):
        """The crash path: Blender exits before the runner writes result.json."""
        before = _scratch("bgate_blender_")
        monkeypatch.setattr(blender, "find_blender", lambda: "blender.exe")
        monkeypatch.setattr(
            blender.subprocess, "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "segfault"))
        got = blender.run_script("pass", out_dir=str(tmp_path), timeout=1)
        assert got["ok"] is False
        assert "without producing a result" in got["error"]
        assert _scratch("bgate_blender_") == before

    def test_blender_run_script_cleans_up_when_it_raises(self, monkeypatch,
                                                         tmp_path):
        """A missing blend_file raises AFTER the scratch dir exists."""
        before = _scratch("bgate_blender_")
        monkeypatch.setattr(blender, "find_blender", lambda: "blender.exe")
        with pytest.raises(FileNotFoundError):
            blender.run_script("pass", blend_file=str(tmp_path / "ghost.blend"),
                               out_dir=str(tmp_path))
        assert _scratch("bgate_blender_") == before


class TestEnvCacheInvalidation:
    """A key pasted into .env used to need a server restart to take effect —
    with nothing on screen saying so."""

    @pytest.fixture(autouse=True)
    def clean(self):
        envfile.reset_cache()
        before = dict(os.environ)
        yield
        for name in set(os.environ) - set(before):
            os.environ.pop(name, None)
        os.environ.update(before)
        envfile.reset_cache()

    def test_a_key_added_after_the_first_load_is_seen(self, tmp_path):
        """The actual reported failure: OPENAI_API_KEY pasted in while the
        server runs."""
        os.environ.pop("BGATE_FAKE_KEY", None)
        env = tmp_path / ".env"
        env.write_text("BGATE_OTHER_SETTING=1\n", encoding="utf-8")
        assert envfile.load_project_env(tmp_path) == ["BGATE_OTHER_SETTING"]

        env.write_text("BGATE_OTHER_SETTING=1\nBGATE_FAKE_KEY=sk-added-live\n",
                       encoding="utf-8")
        assert envfile.load_project_env(tmp_path) == ["BGATE_FAKE_KEY"]
        assert os.environ["BGATE_FAKE_KEY"] == "sk-added-live"

    def test_an_unchanged_file_is_not_reread(self, tmp_path):
        """The hot path: every tool call comes through here, so a no-op must
        stay a stat()."""
        (tmp_path / ".env").write_text("BGATE_FAKE_KEY=v1\n", encoding="utf-8")
        assert envfile.load_project_env(tmp_path) == ["BGATE_FAKE_KEY"]
        for _ in range(3):
            assert envfile.load_project_env(tmp_path) == []

    def test_shell_still_wins_over_a_reloaded_file(self, tmp_path, monkeypatch):
        """Invalidation must not turn .env into an override of the shell."""
        monkeypatch.setenv("BGATE_FAKE_KEY", "from-shell")
        env = tmp_path / ".env"
        env.write_text("BGATE_FAKE_KEY=v1\n", encoding="utf-8")
        envfile.load_project_env(tmp_path)
        env.write_text("BGATE_FAKE_KEY=v2-changed\n", encoding="utf-8")
        assert envfile.load_project_env(tmp_path) == []
        assert os.environ["BGATE_FAKE_KEY"] == "from-shell"

    def test_a_deleted_env_file_is_picked_up_too(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("BGATE_FAKE_KEY=v1\n", encoding="utf-8")
        envfile.load_project_env(tmp_path)
        env.unlink()
        assert envfile.load_project_env(tmp_path) == []
        env.write_text("BGATE_SECOND_KEY=v2\n", encoding="utf-8")
        assert envfile.load_project_env(tmp_path) == ["BGATE_SECOND_KEY"]

    def test_the_value_is_never_returned_logged_or_printed(self, tmp_path,
                                                           caplog, capsys):
        """Reload doubles the chances of a value escaping. It must not."""
        secret = "sk-do-not-leak-this-value"
        env = tmp_path / ".env"
        env.write_text("BGATE_OTHER_SETTING=1\n", encoding="utf-8")
        envfile.load_project_env(tmp_path)

        with caplog.at_level(logging.DEBUG):
            env.write_text(f"BGATE_OTHER_SETTING=1\nBGATE_FAKE_KEY={secret}\n",
                           encoding="utf-8")
            loaded = envfile.load_project_env(tmp_path)

        assert loaded == ["BGATE_FAKE_KEY"]
        assert secret not in str(loaded)
        assert secret not in caplog.text
        captured = capsys.readouterr()
        assert secret not in captured.out + captured.err


class TestCharacterWorkAvoidsTheStyleModel:
    """A style reference follows a LOOK; it does not hold a SUBJECT through a
    pose change. krea.py has recorded that difference since the edit models
    landed — and then routed sprites past it.

    KEYED_KINDS treats sprite/sheet/portrait as character art. CHARACTER_KINDS
    listed only anchor/animation, so those three fell through to DEFAULT_MODEL,
    which is the style model the pin exists to avoid. Measured on a 16-frame
    walk sheet: the style model failed the alpha audit at 14% hollow interior
    and produced no direction change between rows.
    """

    def test_every_kind_that_carries_an_identity_uses_the_edit_model(self):
        from bgate_adapters import krea

        for kind in ("anchor", "animation", "sprite", "sheet", "portrait"):
            assert krea.model_for(kind) == krea.CHARACTER_MODEL, kind

    def test_kinds_with_no_pose_continuity_are_left_on_the_default(self):
        """Deliberately narrow: a prop has no identity to hold."""
        from bgate_adapters import krea

        for kind in ("item", "prop", "icon", "vfx", "concept", ""):
            assert krea.model_for(kind) == krea.DEFAULT_MODEL, kind

    def test_the_character_kinds_are_a_subset_of_the_keyed_kinds(self):
        """A kind routed as character work that the keyer does not recognise
        would come back with an opaque background and no alpha at all."""
        from bgate_adapters import krea
        from bgate_core import chroma

        assert krea.CHARACTER_KINDS <= chroma.KEYED_KINDS
