"""Export-breaking patterns — a static lint pass over ``*.gd`` for faults the
EDITOR never shows, because the editor never runs an export.

EXIT 67's export shipped EMPTY. Every QA gate that ran against it ran in the
editor, against the loose project directory, where these three patterns are
invisible:

  1. Eight scripts listed a data folder with ``DirAccess.get_files()`` (or
     ``list_dir_begin``/``get_next``) and filtered the result with
     ``ends_with(".tres")`` (or ``.tscn``/``.res``/``.gd``). In the editor
     that filter matches. In an EXPORTED pck, every file Godot's remap system
     tracks is listed as ``foo.tres.remap`` — the literal filename on disk —
     so ``ends_with(".tres")`` matches NOTHING and the folder read as empty.
     The fix ships as ``src/templates/godot/2d/scripts/res_dir.gd``
     (``ResDir.files(path, ext)``), which strips ``.remap``/``.import``
     before comparing.

  2. A literal U+FEFF (BOM) INSIDE a GDScript string, such as
     ``begins_with("\ufeff")``. The editor's text view renders it invisibly
     and the check passes. Godot's EXPORTED binary token stream re-encodes
     the string and the BOM is dropped, so ``begins_with("")`` is what
     actually ships — and downstream ``substr(1)`` calls built against the
     BOM's presence eat the first real byte of every string they touch
     (measured: the first byte of every loaded JSON file).

  3. ``FileAccess.open("res://...")`` of a non-resource extension
     (``.json``/``.txt``/``.csv``/...). This reads fine in the editor, where
     the whole project directory is on disk. In an export, only files the
     export's ``include_filter`` names are packed; a JSON data file with no
     matching filter entry is not in the pck and the open fails at runtime.
     Reported ADVISORY, not blocking, because whether it actually breaks
     depends on ``export_presets.cfg`` — which this module also reads when
     given a project directory.

Pure functions over source TEXT so a test can hand this doctored snippets
with no Godot project on disk; :func:`lint_project` adds the filesystem walk
and the ``export_presets.cfg`` cross-check.
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Optional

#: Extensions whose files are remapped on export — the literal on-disk name
#: gains a `.remap` (uncompressed) or the resource itself becomes a `.import`
#: sidecar and the SOURCE keeps its extension in the exported pck's listing
#: for some import types. The measured failure was `.tres`/`.tscn`/`.res`
#: filtered directly; `.gd` scripts are byte-compiled and can carry the same
#: filter bug if a project lists its own scripts at runtime.
REMAPPED_SUFFIXES = (".tres", ".tscn", ".res", ".gd")

#: Non-resource extensions a project commonly reads with FileAccess and that
#: an export's include_filter must be told about explicitly.
DATA_SUFFIXES = (".json", ".txt", ".csv", ".cfg", ".ini", ".xml", ".yaml", ".yml")

#: Zero-width / BOM code points that render invisibly in an editor but are
#: real bytes (or absent bytes, after export re-encoding) in the string.
ZERO_WIDTH_CODEPOINTS = {
    0xFEFF: "U+FEFF ZERO WIDTH NO-BREAK SPACE / BOM",
    0x200B: "U+200B ZERO WIDTH SPACE",
    0x200C: "U+200C ZERO WIDTH NON-JOINER",
    0x200D: "U+200D ZERO WIDTH JOINER",
    0x2060: "U+2060 WORD JOINER",
    0xFFFE: "U+FFFE (byte-order-mark artifact)",
}

BLOCKING = "blocking"
ADVISORY = "advisory"

_LISTING_CALL = re.compile(
    r"\b(get_files_at|get_files|list_dir_begin|get_next)\s*\(")
_ENDS_WITH = re.compile(r'ends_with\s*\(\s*"([^"]*)"\s*\)')
_STRING_LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)"')
_FILEACCESS_OPEN = re.compile(
    r'FileAccess\.open\s*\(\s*"(res://[^"]*)"')


def _finding(*, file: str, line: int, kind: str, severity: str, message: str,
            fix: str, snippet: str = "") -> dict:
    return {
        "file": file, "line": line, "kind": kind, "severity": severity,
        "message": message, "fix": fix, "snippet": snippet.strip(),
        "at": f"{file}:{line}",
    }


def lint_text(path: str, text: str) -> list[dict]:
    """Every finding in one script's source. ``path`` is used only to label them."""
    lines = text.splitlines()
    out: list[dict] = []

    # (a) get_files()/list_dir_begin filtered by a remapped extension with no
    # .remap strip ANYWHERE in the file. A per-line check cannot tell whether
    # the strip happens before or after the ends_with call — a file that
    # strips .remap once and filters twice is safe everywhere it filters — so
    # this is scoped to "does this file know about .remap at all".
    has_listing_call = bool(_LISTING_CALL.search(text))
    strips_remap = ".remap" in text
    if has_listing_call and not strips_remap:
        for i, line in enumerate(lines, start=1):
            match = _ENDS_WITH.search(line)
            if not match:
                continue
            ext = match.group(1)
            if ext in REMAPPED_SUFFIXES:
                out.append(_finding(
                    file=path, line=i, kind="remap_filter", severity=BLOCKING,
                    message=(f'ends_with("{ext}") filters a directory listing '
                             "that will be empty in an EXPORTED pck: exported "
                             f"files are listed as name{ext}.remap, and this "
                             "ends_with never matches that."),
                    fix=('use ResDir.files(path, "' + ext + '") '
                         "(src/templates/godot/2d/scripts/res_dir.gd), which "
                         "strips .remap/.import before comparing, instead of "
                         "get_files()/list_dir_begin + ends_with"),
                    snippet=line))

    # (b) zero-width / BOM code points inside a string literal.
    for i, line in enumerate(lines, start=1):
        for str_match in _STRING_LITERAL.finditer(line):
            literal = str_match.group(1)
            for ch in literal:
                cp = ord(ch)
                if cp in ZERO_WIDTH_CODEPOINTS:
                    out.append(_finding(
                        file=path, line=i, kind="zero_width_literal",
                        severity=BLOCKING,
                        message=(f"string literal contains "
                                 f"{ZERO_WIDTH_CODEPOINTS[cp]}, invisible in "
                                 "an editor. Godot's exported binary token "
                                 "stream can re-encode or drop it, so a check "
                                 "like begins_with(\"\\ufeff\") silently "
                                 "becomes begins_with(\"\") after export, and "
                                 "a paired substr(1) then eats the first real "
                                 "byte of every string it touches."),
                        fix=("remove the zero-width code point from the "
                             "literal; if it is meant to detect a UTF-8 BOM "
                             "on FILE CONTENT, strip it from the loaded "
                             "string with a byte check "
                             "(data.begins_with(PackedByteArray([0xEF,0xBB,"
                             "0xBF]))) rather than a source-code string"),
                        snippet=line))

    # (c) FileAccess.open of a non-resource, non-remapped data extension.
    for i, line in enumerate(lines, start=1):
        match = _FILEACCESS_OPEN.search(line)
        if not match:
            continue
        res_path = match.group(1)
        lowered = res_path.lower()
        if lowered.endswith(DATA_SUFFIXES):
            out.append(_finding(
                file=path, line=i, kind="unfiltered_data_read",
                severity=ADVISORY,
                message=(f'FileAccess.open("{res_path}") reads a '
                         f"non-resource file. This works in the editor "
                         "(the whole project directory is on disk) and can "
                         "fail in an export if export_presets.cfg's "
                         "include_filter does not name this file — the file "
                         "simply is not in the pck."),
                fix=(f"check export_presets.cfg's include_filter covers "
                     f"{res_path} (or its extension pattern, e.g. "
                     f"*.{lowered.rsplit('.', 1)[-1]}), then verify with "
                     "godot_export_verify against the actual exported pck, "
                     "not the project directory"),
                snippet=line))

    return out


def _iter_gd_files(project_dir: str) -> Iterable[str]:
    for dirpath, dirnames, filenames in os.walk(project_dir):
        # Skip the engine's own import cache — it holds generated .gd stubs
        # for some resource types, never hand-written and never exported as
        # source.
        dirnames[:] = [d for d in dirnames if d != ".godot"]
        for name in filenames:
            if name.endswith(".gd"):
                yield os.path.join(dirpath, name)


def _include_filter_covers(project_dir: str, res_path: str) -> Optional[bool]:
    """Best-effort: does export_presets.cfg's include_filter mention this
    file or a glob that would? None when there is no export_presets.cfg to
    read at all — a project with no export preset yet is not a lint failure.
    """
    cfg_path = os.path.join(project_dir, "export_presets.cfg")
    if not os.path.isfile(cfg_path):
        return None
    try:
        with open(cfg_path, "r", encoding="utf-8", errors="replace") as fh:
            cfg = fh.read()
    except OSError:
        return None
    match = re.search(r'include_filter\s*=\s*"([^"]*)"', cfg)
    if not match:
        return False
    patterns = [p.strip() for p in match.group(1).split(",") if p.strip()]
    if not patterns:
        return False
    name = res_path.rsplit("/", 1)[-1]
    ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    for pattern in patterns:
        if pattern in (name, f"*{ext}") or pattern.endswith(ext.lstrip(".")):
            return True
    return False


def lint_project(project_dir: str) -> dict:
    """Every ``*.gd`` under ``project_dir``, linted. ``{ok, findings, scanned}``."""
    findings: list[dict] = []
    scanned = 0
    for path in _iter_gd_files(project_dir):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:                                    # noqa: BLE001
            findings.append(_finding(
                file=path, line=0, kind="unreadable", severity=ADVISORY,
                message=f"could not read: {exc}", fix="", snippet=""))
            continue
        scanned += 1
        rel = os.path.relpath(path, project_dir).replace("\\", "/")
        for row in lint_text(rel, text):
            if row["kind"] == "unfiltered_data_read":
                covered = _include_filter_covers(
                    project_dir, row["snippet"].split('"')[1]
                    if '"' in row["snippet"] else "")
                if covered is True:
                    row["severity"] = ADVISORY
                    row["message"] += " (export_presets.cfg's include_filter " \
                        "appears to cover it — verify with godot_export_verify " \
                        "regardless, since a filter MATCH is not proof the pck " \
                        "was built after it was added)"
                elif covered is False:
                    row["message"] += " export_presets.cfg's include_filter " \
                        "does NOT appear to cover it."
            findings.append(row)
    blocking = [f for f in findings if f["severity"] == BLOCKING]
    return {
        "ok": not blocking,
        "scanned": scanned,
        "findings": findings,
        "blocking": len(blocking),
        "advisory": len(findings) - len(blocking),
    }

