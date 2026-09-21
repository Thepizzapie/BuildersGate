"""Export-breaking patterns lint - fires on the MEASURED EXIT 67 failures.

Every test doctors a snippet with the exact bug, then asserts the lint FIRES
on it, and that the equivalent fixed snippet does not.
"""
from __future__ import annotations

from bgate_core.qa import exportlint


class TestRemapFilter:
    def test_ends_with_tres_with_no_remap_strip_fires(self):
        src = (
            'func _load_all():\n'
            '    for f in DirAccess.get_files_at("res://data"):\n'
            '        if f.ends_with(".tres"):\n'
            '            print(f)\n'
        )
        findings = exportlint.lint_text("data_loader.gd", src)
        kinds = [f["kind"] for f in findings]
        assert "remap_filter" in kinds
        row = next(f for f in findings if f["kind"] == "remap_filter")
        assert row["line"] == 3
        assert "ResDir.files" in row["fix"]

    def test_resdir_files_pattern_does_not_fire(self):
        # The fix: strips .remap before comparing, so the same call shape
        # is not flagged once the file demonstrably handles the remap.
        src = (
            'func _load_all():\n'
            '    for f in ResDir.files("res://data", ".tres"):\n'
            '        print(f)\n'
            '# stripped .remap in ResDir.files already\n'
        )
        findings = exportlint.lint_text("data_loader.gd", src)
        assert not any(f["kind"] == "remap_filter" for f in findings)

    def test_a_manual_remap_strip_clears_it(self):
        src = (
            'func _load_all():\n'
            '    for raw in DirAccess.get_files_at("res://data"):\n'
            '        var f = raw.replace(".remap", "")\n'
            '        if f.ends_with(".tres"):\n'
            '            print(f)\n'
        )
        findings = exportlint.lint_text("data_loader.gd", src)
        assert not any(f["kind"] == "remap_filter" for f in findings)

    def test_list_dir_begin_is_also_covered(self):
        src = (
            'func _walk():\n'
            '    var d = DirAccess.open("res://data")\n'
            '    d.list_dir_begin()\n'
            '    var f = d.get_next()\n'
            '    if f.ends_with(".tscn"):\n'
            '        pass\n'
        )
        findings = exportlint.lint_text("walker.gd", src)
        assert any(f["kind"] == "remap_filter" for f in findings)

    def test_ends_with_import_is_not_a_remapped_suffix(self):
        # .import sidecars are not resource files themselves; filtering FOR
        # them is a different (unusual but not export-breaking) thing.
        src = (
            'func _load_all():\n'
            '    for f in DirAccess.get_files_at("res://data"):\n'
            '        if f.ends_with(".import"):\n'
            '            pass\n'
        )
        findings = exportlint.lint_text("data_loader.gd", src)
        assert not any(f["kind"] == "remap_filter" for f in findings)


class TestZeroWidthLiteral:
    def test_bom_in_string_literal_fires(self):
        src = 'func _has_bom(s: String) -> bool:\n\treturn s.begins_with("﻿")\n'
        findings = exportlint.lint_text("bom.gd", src)
        row = next(f for f in findings if f["kind"] == "zero_width_literal")
        assert row["line"] == 2
        assert "U+FEFF" in row["message"]

    def test_ordinary_string_does_not_fire(self):
        src = 'func _has_bom(s: String) -> bool:\n\treturn s.begins_with("x")\n'
        findings = exportlint.lint_text("bom.gd", src)
        assert not any(f["kind"] == "zero_width_literal" for f in findings)

    def test_zero_width_space_also_fires(self):
        src = 'var label := "a​b"\n'
        findings = exportlint.lint_text("labels.gd", src)
        assert any(f["kind"] == "zero_width_literal" for f in findings)


class TestUnfilteredDataRead:
    def test_fileaccess_open_json_is_advisory(self):
        src = 'func _load():\n\treturn FileAccess.open("res://data/quests.json", FileAccess.READ)\n'
        findings = exportlint.lint_text("quests.gd", src)
        row = next(f for f in findings if f["kind"] == "unfiltered_data_read")
        assert row["severity"] == exportlint.ADVISORY
        assert row["line"] == 2

    def test_fileaccess_open_tres_does_not_fire(self):
        # .tres is a resource type Godot's export pipeline tracks on its own;
        # this check is scoped to non-resource data extensions.
        src = 'func _load():\n\treturn FileAccess.open("res://data/quests.tres", FileAccess.READ)\n'
        findings = exportlint.lint_text("quests.gd", src)
        assert not any(f["kind"] == "unfiltered_data_read" for f in findings)

    def test_an_advisory_finding_never_fails_ok_on_its_own(self, tmp_path):
        (tmp_path / "quests.gd").write_text(
            'func _load():\n\treturn FileAccess.open("res://data/quests.json", '
            'FileAccess.READ)\n', encoding="utf-8")
        result = exportlint.lint_project(str(tmp_path))
        assert result["ok"]
        assert result["advisory"] == 1
        assert result["blocking"] == 0


class TestLintProject:
    def test_a_blocking_finding_fails_ok(self, tmp_path):
        (tmp_path / "loader.gd").write_text(
            'func _load_all():\n'
            '    for f in DirAccess.get_files_at("res://data"):\n'
            '        if f.ends_with(".tres"):\n'
            '            pass\n', encoding="utf-8")
        result = exportlint.lint_project(str(tmp_path))
        assert not result["ok"]
        assert result["blocking"] == 1
        assert result["scanned"] == 1

    def test_a_clean_project_passes(self, tmp_path):
        (tmp_path / "loader.gd").write_text(
            'func _load_all():\n'
            '    for f in ResDir.files("res://data", ".tres"):\n'
            '        pass\n'
            '# strips .remap via ResDir\n', encoding="utf-8")
        result = exportlint.lint_project(str(tmp_path))
        assert result["ok"]
        assert result["blocking"] == 0

    def test_dot_godot_cache_is_not_scanned(self, tmp_path):
        cache = tmp_path / ".godot" / "generated"
        cache.mkdir(parents=True)
        (cache / "stub.gd").write_text(
            'func _load_all():\n'
            '    for f in DirAccess.get_files_at("res://data"):\n'
            '        if f.ends_with(".tres"):\n'
            '            pass\n', encoding="utf-8")
        result = exportlint.lint_project(str(tmp_path))
        assert result["scanned"] == 0
        assert result["ok"]
